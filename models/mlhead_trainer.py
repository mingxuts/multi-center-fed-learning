import importlib
import numpy as np
import os, logging
import sys
import time
import pickle
import copy

import random
import tensorflow as tf
from baseline_constants import ACCURACY_KEY

#logging.disable(logging.WARNING)
#os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

import metrics.writer as metrics_writer
STAT_METRICS_PATH = 'metrics/stat_metrics.csv'
SYS_METRICS_PATH = 'metrics/sys_metrics.csv'


# below import fedmc necessary lib
from mh_constants import VARIABLE_PARAMS
from mlhead_clus_server import Mlhead_Clus_Server
from mlhead_utilfuncs import get_tensor_from_localmodels,count_num_point_from, log_history, save_historyfile

from baseline_constants import MAIN_PARAMS, MODEL_PARAMS
from mlhead_client import Client
from server import Server
from model import ServerModel
from fedprox_optimizer import PerturbedGradientDescent

def get_stat_writer_function(ids, groups, num_samples, args):

    def writer_fn(num_round, metrics, partition):
        metrics_writer.print_metrics(
            num_round, ids, metrics, groups, num_samples, partition, args.metrics_dir, '{}_{}'.format(args.metrics_name, 'stat'))

    return writer_fn


def get_sys_writer_function(args):

    def writer_fn(num_round, ids, metrics, groups, num_samples):
        metrics_writer.print_metrics(
            num_round, ids, metrics, groups, num_samples, 'train', args.metrics_dir, '{}_{}'.format(args.metrics_name, 'sys'))

    return writer_fn


def mlhead_print_totloss(k, eval_every, rounds, prefix, accuracy, cluster, stack_list, client_list):
    print("acc list dimension: ", accuracy.ndim)
    micro_acc = np.mean(accuracy)
    print('micro (with weight) test_%s: %g' % (prefix, micro_acc) )
    #save_metric_csv(k+1, micro_acc, stack_list)
    macro_acc = np.mean([stack_list[cl] for cl in stack_list])
    print('macro (overall) test_%s: %g' % (prefix, macro_acc) )
    log_history(k+1, micro_acc, macro_acc, client_list)


def mlhead_print_stats(
    num_round, server, clients, num_samples, args, writer, stack_list, prepare_test, acc_array = None):
    
    train_stat_metrics = server.test_model(clients, exec_prepare_test=False, set_to_use='train')
    print_metrics(train_stat_metrics, num_samples, prefix='train_')
    writer(num_round, train_stat_metrics, 'train')
    test_stat_metrics = server.test_model(clients, exec_prepare_test=prepare_test, set_to_use='test')
    for k in test_stat_metrics:
        stack_list[k] = test_stat_metrics[k][ACCURACY_KEY]
  
    test_acc =  print_metrics(test_stat_metrics, num_samples, prefix='test_')
    writer(num_round, test_stat_metrics, 'test')
    # We also wants to evaluate a macro value (accuracy & loss)
    c = max(args.num_clusters, 1)
    if acc_array is not None:
        return np.append([acc_array], [test_acc])
    else:
        return np.array(test_acc)

def print_metrics(metrics, weights, prefix=''):
    """Prints weighted averages of the given metrics.
    """
    ordered_weights = [weights[c] for c in sorted(weights)]
    metric_names = metrics_writer.get_metrics_names(metrics)
    
    micro_acc = metric_names[0]
    miacc_metric = [metrics[c][micro_acc] for c in sorted(metrics)]
    to_ret = np.average(miacc_metric, weights=ordered_weights)
    for metric in metric_names[:2]:
        ordered_metric = [metrics[c][metric] for c in sorted(metrics)]
        print('%s: %g, 10th percentile: %g, 50th percentile: %g, 90th percentile %g' \
              % (prefix + metric,
                 np.average(ordered_metric, weights=ordered_weights),
                 np.percentile(ordered_metric, 10),
                 np.percentile(ordered_metric, 50),
                 np.percentile(ordered_metric, 90)))

    return to_ret

class MlheadTrainer():
    
    def __init__(self, args, users, groups, train_data, test_data): 
        # Set the random seed if provided (affects client sampling, and batching)
        random.seed(55 + args.seed)
        np.random.seed(555 + args.seed)
        tf.set_random_seed(444 + args.seed)

        model_path = '%s/%s.py' % (args.dataset, args.model)
        if not os.path.exists(model_path):
            print('Please specify a valid dataset and a valid model.')
        model_path = '%s.%s' % (args.dataset, args.model)

        print('############################## %s ##############################' % model_path)
        mod = importlib.import_module(model_path)
        ClientModel = getattr(mod, 'ClientModel')

        tup = MAIN_PARAMS[args.dataset][args.t]
        self.num_rounds = args.num_rounds if args.num_rounds != -1 else tup[0]
        self.eval_every = args.eval_every if args.eval_every != -1 else tup[1]
        self.clients_per_round = args.clients_per_round if args.clients_per_round != -1 else tup[2]

        model_params = MODEL_PARAMS[model_path]
        if args.lr != -1:
            model_params_list = list(model_params)
            model_params_list[0] = args.lr
            model_params = tuple(model_params_list)

        # Create client model, and share params with server model
        tf.reset_default_graph()
        if (args.model.endswith("_prox")):
            optimizer = PerturbedGradientDescent(model_params[0], args.mu)
        else:
            optimizer = None
        client_model = ClientModel(args.seed, *model_params, optimizer)

        # Create clients
        self.clients = self.setup_clients(args.dataset, args.model, users, groups, train_data, test_data, client_model)
        client_ids, client_groups, client_num_samples = Server().get_clients_info(self.clients)
        print('Clients in Total: %d' % len(self.clients))
        # Initial status
        print('--- Random Initialization ---')
        self.stat_writer_fn = get_stat_writer_function(client_ids, client_groups, client_num_samples, args)
        sys_writer_fn = get_sys_writer_function(args)
        
#         self.head_server_stack = [Server(client_model) for _ in range(max(args.num_clusters, 1))]
        print("--- Do training and initilized clusting server---")
        self.mlhead_cluster = Mlhead_Clus_Server(client_model, args.dataset, args.model, args.num_clusters, len(self.clients))
        self.mlhead_cluster.select_clients(15896001, self.clients)
        #print("Client models are saved at %s"  % self.mlhead_cluster.path ) 
        self.center_models = [None] * args.num_clusters
        self.center_init(args.num_clusters, client_model)

    def center_init(self, num_clusters, client_model):
        for i in range(num_clusters):
            if os.path.exists("cnn-C{}.pb".format(i)):
                with open("cnn-C{}.pb".format(i), "rb") as f:
                    self.center_models[i] = pickle.load(f)
            else:
                self.center_models[i] = copy.deepcopy(client_model.get_params())
                
    def default_cluster(self):
        group = len(self.clients), self.clients
        default_list = list()
        default_list.append(group)
        return default_list
    
    def clustering_function(self, points):
        start_time = time.time()
        # comment one clustering to use another
        # this is outlier ones
        iter_stop = 0
        learned_cluster = self.mlhead_cluster.outlier_clustering(points)
        while (self.mlhead_cluster.is_unbalanced_clus(learned_cluster)) and (iter_stop < 2):
            iter_stop += 1
            learned_cluster = self.mlhead_cluster.outlier_clustering(points)
            
        # this is simple ones
        #learned_cluster = self.mlhead_cluster.run_clustering(points)
        end_time = time.time() - start_time
        self.kmeans_cost.append(end_time) 
        return learned_cluster
        
    def setup_clients(self, dataset, model_dir, users, groups, train_data, test_data, model):
        """
        Return:
            all_clients: list of Client objects.
        """    
        if len(groups) == 0:
            groups = [[] for _ in users]
        write_to_path =  os.path.join('/scratch/leaf/ckpt_runtime', dataset, model_dir)
        if not os.path.exists(write_to_path):
            os.makedirs(write_to_path)
        all_clients = [Client(u, g, train_data[u], test_data[u], model, write_to_path) for u, g in zip(users, groups)]
        return all_clients
        
    def train(self, args):
        """
            A trainer different from the baseline
            Then all need to do is replace different optimizer
        """

        # Simulate training
        print("----- Multi-center Federated Training -----")
        prev_score = None
        self.kmeans_cost = []
        for k in range(self.num_rounds):
            best_kept = None
            stack_list = {}
            client_list = {}
            if prev_score is None: # This is the first iteration
                if args.num_clusters == -1 :
                    write_file = False
                    learned_cluster = self.default_cluster()
                else:
                    print("----- First time center rendering  -----")
                    write_file = True
                    
                # do kmeans clustering using algorithm with the points above as input, 
                # the result is three groups of clients, and their centroids
                # their centroids is the reduction result, so we can't use them as the direct model parameters, 
                # cluster is in this form: a list of (num_clients, clients), client is a array of client model.             
                    c_wts = self.mlhead_cluster.get_init_point_data()
                    learned_cluster = self.clustering_function(c_wts)
                    prev_score = len(c_wts)


            #print('--- Round %d of %d: Training %d Clients ---' % (k + 1, num_rounds, clients_per_round))
            print('--- Round %d of %d: Training assigned to %d Cluster ' % (k + 1, self.num_rounds, len(learned_cluster)), 
                "<", count_num_point_from(learned_cluster), "> ---")

            joined_clients = dict()
            for c_idx, group in enumerate(learned_cluster):
                server = Server()
                if group[0] <= 1:
                    print("Skip cluster %d as number of client not enough" % c_idx)
                    continue
                # if not explicit clients per round given, then default all
                # clients in this group participarte training
                if self.clients_per_round != -1: 
                    num = min(self.clients_per_round, group[0])
                    active_clients = np.random.choice(group[1], num, replace=False)
                else:
                    active_clients = group[1]
                
                client_list[c_idx] = [c.id for c in active_clients ]
                c_ids, c_groups, c_num_samples = server.get_clients_info(active_clients)
                sys_metrics, updates = server.train_model(self.center_models[c_idx], num_epochs=args.num_epochs, batch_size=args.batch_size, minibatch=args.minibatch, clients = active_clients)
                sys_fn = get_sys_writer_function(args)
                sys_fn(k + 1, c_ids, sys_metrics, c_groups, c_num_samples)
                
                for c, up in zip(active_clients, updates):
                    joined_clients[c.id] = up[1]
                # Thinking how to do a distance as weight averging
                if args.weight_mode == "no_size":
                    self.center_models[c_idx] = server.update_model_wmode()
                else:
                    server.update_model_nowmode()

                #_, _, client_num_samples = server.get_clients_info(active_clients)
                self.stat_writer_fn = get_stat_writer_function(c_ids, c_groups, c_num_samples, args)
                best_kept = mlhead_print_stats(k + 1, server, active_clients, c_num_samples, args, 
                                                   self.stat_writer_fn, stack_list, True, best_kept)
            # end iterate clusters
            mlhead_print_totloss(
                k, self.eval_every, self.num_rounds, "accuracy",  best_kept, learned_cluster, stack_list, client_list)

            # Update the center point when k = local training * a mulitplier
            if not args.num_clusters == -1 and not k == (self.num_rounds -1) and (k + 1) % args.update_head_every == 0:
                c_wts = get_tensor_from_localmodels(joined_clients,
                                                  c_wts,
                                                  self.mlhead_cluster.variable, self.clients[0].model)
                learned_cluster = self.clustering_function(c_wts) # cwts is N (clients) x x_dimensions
                joined_clients.clear()
                print("----- Multi-headed clustering performed -----")
                prev_score = len(c_wts)
                

    def finish(self, args):
        # save history file
        save_historyfile()
        # Save server model        
#         ckpt_path = os.path.join('checkpoints', args.dataset)
#         if not os.path.exists(ckpt_path):
#             os.makedirs(ckpt_path)

#         for i, server in enumerate(self.head_server_stack):
#             # {}-K{}-C{}, K stands for number of clusters and C stands for ith center
#             save_path = server.save_model(os.path.join(ckpt_path, '{}-K{}-C{}.ckpt'.format(args.model, args.num_clusters, i+1)))
#         print('Model saved in path: %s' % save_path)
        print('{} rounds kmeans used {:.3f}'.format(self.num_rounds, np.average(self.kmeans_cost, weights=None)))
#         for i, server in enumerate(self.center_models):
#             head_weights = server
#             with open('./{}-C{}.pb'.format(args.model, i), 'wb+') as f:
#                 pickle.dump(head_weights, f)
                
#         for s in self.head_server_stack:
#             s.close_model()
