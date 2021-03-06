import copy
import numpy as np
import time

from lapsolver import solve_dense

def layerwise_sampler(batch_weights, layer_index, batch_frequencies, sigma_layers, 
                                sigma0_layers, gamma_layers, it, 
                                model_meta_data, 
                                model_layer_type,
                                n_layers,
                                matching_shapes):
    """
    We implement a layer-wise matching here:
    """
    if type(sigma_layers) is not list:
        sigma_layers = (n_layers - 1) * [sigma_layers]
    if type(sigma0_layers) is not list:
        sigma0_layers = (n_layers - 1) * [sigma0_layers]
    if type(gamma_layers) is not list:
        gamma_layers = (n_layers - 1) * [gamma_layers]

    last_layer_const = []
    total_freq = np.array(batch_frequencies).sum()
#     print("what holds in b_freq: {}, total: {}".format(batch_frequencies, total_freq))
    for f in batch_frequencies:
        last_layer_const.append(f / total_freq)

    # J: number of workers
    J = len(batch_weights)
    init_num_kernel = batch_weights[0][0].shape[0]

    # for saving (#channel * k * k)
    init_channel_kernel_dims = []
    for bw in batch_weights[0]:
        if len(bw.shape) > 1:
            init_channel_kernel_dims.append(bw.shape[1])
    #print("init_channel_kernel_dims: {}".format(init_channel_kernel_dims))
    
    sigma_bias_layers = sigma_layers
    sigma0_bias_layers = sigma0_layers
    mu0 = 0.
    mu0_bias = 0.1
    assignment_c = [None for j in range(J)]
    L_next = None

    sigma = sigma_layers[layer_index - 1]
    sigma_bias = sigma_bias_layers[layer_index - 1]
    gamma = gamma_layers[layer_index - 1]
    sigma0 = sigma0_layers[layer_index - 1]
    sigma0_bias = sigma0_bias_layers[layer_index - 1]
    first_fc_name = "dense/kernel"

    if layer_index <= 1:
        weights_bias = [np.hstack((batch_weights[j][0], batch_weights[j][layer_index * 2 - 1].reshape(-1, 1))) for j in range(J)]

        sigma_inv_prior = np.array(
            init_channel_kernel_dims[layer_index - 1] * [1 / sigma0] + [1 / sigma0_bias])
        mean_prior = np.array(init_channel_kernel_dims[layer_index - 1] * [mu0] + [mu0_bias])

        # handling 2-layer neural network
        if n_layers == 2:
            sigma_inv_layer = [
                np.array(D * [1 / sigma] + [1 / sigma_bias] + [y / sigma for y in last_layer_const[j]]) for j in range(J)]
        else:
            sigma_inv_layer = [np.array(init_channel_kernel_dims[layer_index - 1] * [1 / sigma] + [1 / sigma_bias]) for j in range(J)]

    elif layer_index == (n_layers - 1) and n_layers > 2:
        # our assumption is that this branch will consistently handle the last fc layers
        layer_type = model_layer_type[2 * layer_index - 2]
        prev_layer_type = model_layer_type[2 * layer_index - 2 - 2]
        first_fc_identifier = (layer_type == first_fc_name)
        if first_fc_identifier:
            weights_bias = [np.hstack((batch_weights[j][2 * layer_index - 2], 
                                        batch_weights[j][2 * layer_index - 1].reshape(-1, 1))) for j in range(J)]
        else:
            weights_bias = [np.hstack((batch_weights[j][2 * layer_index - 2], 
                                        batch_weights[j][2 * layer_index - 1].reshape(-1, 1))) for j in range(J)]


        sigma_inv_prior = np.array([1 / sigma0_bias] + (weights_bias[0].shape[1] - 1) * [1 / sigma0])
        mean_prior = np.array([mu0_bias] + (weights_bias[0].shape[1] - 1) * [mu0])
        
        # hwang: this needs to be handled carefully
        #sigma_inv_layer = [np.array([1 / sigma_bias] + [y / sigma for y in last_layer_const[j]]) for j in range(J)]
        #sigma_inv_layer = [np.array([1 / sigma_bias] + (weights_bias[j].shape[1] - 1) * [1 / sigma]) for j in range(J)]

        #sigma_inv_layer = [np.array((matching_shapes[layer_index - 2]) * [1 / sigma] + [1 / sigma_bias] + [y / sigma for y in last_layer_const[j]]) for j in range(J)]
        
        #sigma_inv_layer = [np.array((matching_shapes[layer_index - 2]) * [1 / sigma] + [1 / sigma_bias]) for j in range(J)]
        sigma_inv_layer = [np.array([1 / sigma_bias] + (weights_bias[j].shape[1] - 1) * [1 / sigma]) for j in range(J)]

    elif (layer_index > 1 and layer_index < (n_layers - 1)):
        layer_type = model_layer_type[2 * layer_index - 2]
        prev_layer_type = model_layer_type[2 * layer_index - 2 - 2]

        if 'conv' in layer_type:
            weights_bias = [np.hstack((batch_weights[j][2 * layer_index - 2], batch_weights[j][2 * layer_index - 1].reshape(-1, 1))) for j in range(J)]

        elif 'dense' in layer_type:
            # we need to determine if the type of the current layer is the same as it's previous layer
            # i.e. we need to identify if the fully connected layer we're working on is the first fc layer after the conv block
            first_fc_identifier = (layertype == first_fc_name)
            if first_fc_identifier:
                weights_bias = [np.hstack((batch_weights[j][2 * layer_index - 2].T, batch_weights[j][2 * layer_index - 1].reshape(-1, 1))) for j in range(J)]
            else:
                weights_bias = [np.hstack((batch_weights[j][2 * layer_index - 2].T, batch_weights[j][2 * layer_index - 1].reshape(-1, 1))) for j in range(J)]          

        sigma_inv_prior = np.array([1 / sigma0_bias] + (weights_bias[0].shape[1] - 1) * [1 / sigma0])
        mean_prior = np.array([mu0_bias] + (weights_bias[0].shape[1] - 1) * [mu0])
        sigma_inv_layer = [np.array([1 / sigma_bias] + (weights_bias[j].shape[1] - 1) * [1 / sigma]) for j in range(J)]

    assignment_c, global_weights_c, global_sigmas_c = match_layer(weights_bias, sigma_inv_layer, mean_prior,
                                                                  sigma_inv_prior, gamma, it)

    L_next = global_weights_c.shape[0]

    if layer_index <= 1:
        if n_layers == 2:
            softmax_bias, softmax_inv_sigma = process_softmax_bias(batch_weights, last_layer_const, sigma, sigma0)
            global_weights_out = [softmax_bias]
            global_inv_sigmas_out = [softmax_inv_sigma]
        
        global_weights_out = [global_weights_c[:, :init_channel_kernel_dims[int(layer_index/2)]], global_weights_c[:, init_channel_kernel_dims[int(layer_index/2)]]]
        global_inv_sigmas_out = [global_sigmas_c[:, :init_channel_kernel_dims[int(layer_index/2)]], global_sigmas_c[:, init_channel_kernel_dims[int(layer_index/2)]]]

        print("Branch A, Layer index: {}, Global weights out shapes: {}".format(layer_index, [gwo.shape for gwo in global_weights_out]))

    elif layer_index == (n_layers - 1) and n_layers > 2:
        softmax_bias, softmax_inv_sigma = process_softmax_bias(batch_weights, last_layer_const, sigma, sigma0)

        layer_type = model_layer_type[2 * layer_index - 2]
        prev_layer_type = model_layer_type[2 * layer_index - 2 - 2]
        first_fc_identifier = (layer_type == first_fc_name)
        gwc_shape = global_weights_c.shape
        if "conv" in layer_type:
            global_weights_out = [global_weights_c[:, 0:gwc_shape[1]-1], global_weights_c[:, gwc_shape[1]-1]]
            global_inv_sigmas_out = [global_sigmas_c[:, 0:gwc_shape[1]-1], global_sigmas_c[:, gwc_shape[1]-1]]
        elif "dense" in layer_type:
            global_weights_out = [global_weights_c[:, 0:gwc_shape[1]-1].T, global_weights_c[:, gwc_shape[1]-1]]
            global_inv_sigmas_out = [global_sigmas_c[:, 0:gwc_shape[1]-1].T, global_sigmas_c[:, gwc_shape[1]-1]]

        print("#### Branch B, Layer index: {}, Global weights out shapes: {}".format(layer_index, [gwo.shape for gwo in global_weights_out]))

    elif (layer_index > 1 and layer_index < (n_layers - 1)):
        layer_type = model_layer_type[2 * layer_index - 2]
        gwc_shape = global_weights_c.shape

        if "conv" in layer_type:
            global_weights_out = [global_weights_c[:, 0:gwc_shape[1]-1], global_weights_c[:, gwc_shape[1]-1]]
            global_inv_sigmas_out = [global_sigmas_c[:, 0:gwc_shape[1]-1], global_sigmas_c[:, gwc_shape[1]-1]]
        elif "dense" in layer_type:
            global_weights_out = [global_weights_c[:, 0:gwc_shape[1]-1].T, global_weights_c[:, gwc_shape[1]-1]]
            global_inv_sigmas_out = [global_sigmas_c[:, 0:gwc_shape[1]-1].T, global_sigmas_c[:, gwc_shape[1]-1]]
        print("Branch C, layer index, Layer index: {}, Global weights out shapes: {}".format(layer_index, [gwo.shape for gwo in global_weights_out]))

    print("global inv sigma out shape: {}".format([giso.shape for giso in global_inv_sigmas_out]))
    map_out = [g_w / g_s for g_w, g_s in zip(global_weights_out, global_inv_sigmas_out)]
    return map_out, assignment_c, L_next

def match_layer(weights_bias, sigma_inv_layer, mean_prior, sigma_inv_prior, gamma, it):
    J = len(weights_bias)

    group_order = sorted(range(J), key=lambda x: -weights_bias[x].shape[0])

    batch_weights_norm = [w * s for w, s in zip(weights_bias, sigma_inv_layer)]
    prior_mean_norm = mean_prior * sigma_inv_prior

    global_weights = prior_mean_norm + batch_weights_norm[group_order[0]]
    global_sigmas = np.outer(np.ones(global_weights.shape[0]), sigma_inv_prior + sigma_inv_layer[group_order[0]])

    popularity_counts = [1] * global_weights.shape[0]

    assignment = [[] for _ in range(J)]

    assignment[group_order[0]] = list(range(global_weights.shape[0]))

    ## Initialize
    for j in group_order[1:]:
        global_weights, global_sigmas, popularity_counts, assignment_j = matching_upd_j(batch_weights_norm[j],
                                                                                        global_weights,
                                                                                        sigma_inv_layer[j],
                                                                                        global_sigmas, prior_mean_norm,
                                                                                        sigma_inv_prior,
                                                                                        popularity_counts, gamma, J)
        assignment[j] = assignment_j

    ## Iterate over groups
    for iteration in range(it):
        random_order = np.random.permutation(J)
        for j in random_order:  # random_order:
            to_delete = []
            ## Remove j
            Lj = len(assignment[j])
            for l, i in sorted(zip(range(Lj), assignment[j]), key=lambda x: -x[1]):
                popularity_counts[i] -= 1
                if popularity_counts[i] == 0:
                    del popularity_counts[i]
                    to_delete.append(i)
                    for j_clean in range(J):
                        for idx, l_ind in enumerate(assignment[j_clean]):
                            if i < l_ind and j_clean != j:
                                assignment[j_clean][idx] -= 1
                            elif i == l_ind and j_clean != j:
                                logger.info('Warning - weird unmatching')
                else:
                    global_weights[i] = global_weights[i] - batch_weights_norm[j][l]
                    global_sigmas[i] -= sigma_inv_layer[j]

            global_weights = np.delete(global_weights, to_delete, axis=0)
            global_sigmas = np.delete(global_sigmas, to_delete, axis=0)

            ## Match j
            global_weights, global_sigmas, popularity_counts, assignment_j = matching_upd_j(batch_weights_norm[j],
                                                                                            global_weights,
                                                                                            sigma_inv_layer[j],
                                                                                            global_sigmas,
                                                                                            prior_mean_norm,
                                                                                            sigma_inv_prior,
                                                                                            popularity_counts, gamma, J)
            assignment[j] = assignment_j

    print('Number of global neurons is %d, gamma %f' % (global_weights.shape[0], gamma))
    print("***************Shape of global weights after match: {} ******************".format(global_weights.shape))
    return assignment, global_weights, global_sigmas

def process_softmax_bias(batch_weights, last_layer_const, sigma, sigma0):
    J = len(batch_weights)
    sigma_bias = sigma
    sigma0_bias = sigma0
    mu0_bias = 0.1
    softmax_bias = [batch_weights[j][-1] for j in range(J)]
    softmax_inv_sigma = [s / sigma_bias for s in last_layer_const]
    softmax_bias = sum([b * s for b, s in zip(softmax_bias, softmax_inv_sigma)]) + mu0_bias / sigma0_bias
    softmax_inv_sigma = 1 / sigma0_bias + sum(softmax_inv_sigma)
    return softmax_bias, softmax_inv_sigma


def matching_upd_j(weights_j, global_weights, sigma_inv_j, global_sigmas, prior_mean_norm, prior_inv_sigma,
                   popularity_counts, gamma, J):

    L = global_weights.shape[0]

    compute_cost_start = time.time()
    full_cost = compute_cost(global_weights.astype(np.float32), weights_j.astype(np.float32), global_sigmas.astype(np.float32), sigma_inv_j.astype(np.float32), prior_mean_norm.astype(np.float32), prior_inv_sigma.astype(np.float32),
                             popularity_counts, gamma, J)
    compute_cost_dur = time.time() - compute_cost_start
#     print("###### Compute cost dur: {}".format(compute_cost_dur))

    #row_ind, col_ind = linear_sum_assignment(-full_cost)
    # please note that this can not run on non-Linux systems
    start_time = time.time()
    row_ind, col_ind = solve_dense(-full_cost)
    solve_dur = time.time() - start_time

    assignment_j = []
    new_L = L

    for l, i in zip(row_ind, col_ind):
        if i < L:
            popularity_counts[i] += 1
            assignment_j.append(i)
            global_weights[i] += weights_j[l]
            global_sigmas[i] += sigma_inv_j
        else:  # new neuron
            popularity_counts += [1]
            assignment_j.append(new_L)
            new_L += 1
            global_weights = np.vstack((global_weights, prior_mean_norm + weights_j[l]))
            global_sigmas = np.vstack((global_sigmas, prior_inv_sigma + sigma_inv_j))

    return global_weights, global_sigmas, popularity_counts, assignment_j

def row_param_cost_simplified(global_weights, weights_j_l, sij_p_gs, red_term):
    match_norms = ((weights_j_l + global_weights) ** 2 / sij_p_gs).sum(axis=1) - red_term
    return match_norms

def compute_cost(global_weights, weights_j, global_sigmas, sigma_inv_j, prior_mean_norm, prior_inv_sigma,
                 popularity_counts, gamma, J):

    param_cost_start = time.time()
    Lj = weights_j.shape[0]
    counts = np.minimum(np.array(popularity_counts, dtype=np.float32), 10)

    sij_p_gs = sigma_inv_j + global_sigmas
    red_term = (global_weights ** 2 / global_sigmas).sum(axis=1)
    stupid_line_start = time.time()

    param_cost = np.array([row_param_cost_simplified(global_weights, weights_j[l], sij_p_gs, red_term) for l in range(Lj)], dtype=np.float32)
    stupid_line_dur = time.time() - stupid_line_start

    param_cost += np.log(counts / (J - counts))
    param_cost_dur = time.time() - param_cost_start

    ## Nonparametric cost
    nonparam_start = time.time()
    L = global_weights.shape[0]
    max_added = min(Lj, max(700 - L, 1))
    nonparam_cost = np.outer((((weights_j + prior_mean_norm) ** 2 / (prior_inv_sigma + sigma_inv_j)).sum(axis=1) - (
                prior_mean_norm ** 2 / prior_inv_sigma).sum()), np.ones(max_added, dtype=np.float32))
    cost_pois = 2 * np.log(np.arange(1, max_added + 1))
    nonparam_cost -= cost_pois
    nonparam_cost += 2 * np.log(gamma / J)

    nonparam_dur = time.time() - nonparam_start
    #logger.info("Time cost of param cost: {}, of nonparam cost: {}, stupid line cost: {}, Lj: {}".format(param_cost_dur, 
    #                                nonparam_dur, stupid_line_dur, Lj))

    full_cost = np.hstack((param_cost, nonparam_cost)).astype(np.float32)
    return full_cost