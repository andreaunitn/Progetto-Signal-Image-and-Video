from __future__ import print_function, absolute_import

from scipy.spatial.distance import cosine
from collections import OrderedDict
import numpy as np
import torch
import time

from .feature_extraction import extract_cnn_feature
from .evaluation_metrics import cmc, mean_ap
from .utils.meters import AverageMeter

def k_re_ranking(feat, k1=20, k2=6, lambda_value=0.3):
    # k-reciprocal re-ranking

    # Compute pairwise Euclidean distance
    original_dist = torch.cdist(feat, feat, p=2)

    _, initial_rank = torch.sort(original_dist, dim=1, descending=False)
    gallery_num = original_dist.size(1)

    # Compute k-reciprocal feature
    V = torch.zeros_like(original_dist)
    original_dist_norm = original_dist / torch.max(original_dist, dim=1, keepdim=True).values

    for i in range(original_dist.size(0)):
        forward_k_neigh_index = initial_rank[i, :k1 + 1]
        backward_k_neigh_index = initial_rank[forward_k_neigh_index, :k1 + 1]
        fi = torch.nonzero(backward_k_neigh_index == i)[:, 1]
        k_reciprocal_index = forward_k_neigh_index[fi]
        k_reciprocal_expansion_index = k_reciprocal_index.clone()

        for j in range(k_reciprocal_index.size(0)):
            candidate = k_reciprocal_index[j]
            candidate_forward_k_neigh_index = initial_rank[candidate, :round((k1 + 1) / 2)]
            candidate_backward_k_neigh_index = initial_rank[candidate_forward_k_neigh_index, :round((k1 + 1) / 2)]
            fi_candidate = torch.nonzero(candidate_backward_k_neigh_index == candidate)[:, 1]
            candidate_k_reciprocal_index = candidate_forward_k_neigh_index[fi_candidate]
            intersect = torch.tensor(np.intersect1d(k_reciprocal_index.cpu(), candidate_k_reciprocal_index.cpu()))
            if intersect.size(0) > 2 / 3 * candidate_k_reciprocal_index.size(0):
                k_reciprocal_expansion_index = torch.cat((k_reciprocal_expansion_index, candidate_k_reciprocal_index))

        k_reciprocal_expansion_index = torch.unique(k_reciprocal_expansion_index)
        weight = torch.exp(-original_dist_norm[i, k_reciprocal_expansion_index])
        V[i, k_reciprocal_expansion_index] = weight / torch.sum(weight)

    # Local query expansion
    if k2 != 1:
        V_qe = torch.mean(V[initial_rank[:, :k2], :], dim=1)
        V = V_qe

    # Inverted Index
    invIndex = []
    for i in range(gallery_num):
        invIndex.append(torch.nonzero(V[:, i] != 0).squeeze(dim=1))

    jaccard_dist = torch.zeros_like(original_dist)

    for i in range(original_dist.size(0)):
        temp_min = torch.zeros(gallery_num)
        indNonZero = torch.nonzero(V[i, :] != 0).squeeze(dim=1)
        indImages = [invIndex[indNonZero[j]] for j in range(indNonZero.size(0))]
        for j in range(indNonZero.size(0)):
            temp_min[indImages[j]] += torch.min(V[i, indNonZero[j]], V[indImages[j], indNonZero[j]])
        jaccard_dist[i, :] = 1 - temp_min / (2 - temp_min)

    final_dist = jaccard_dist * (1 - lambda_value) + original_dist * lambda_value
    #final_dist = final_dist[:original_dist.size(0), original_dist.size(0):].transpose(0, 1)

    return final_dist


# def k_reciprocal_re_ranking(distance_matrix, k1=20, k2=6, lambda_value=0.3):
#     # Perform k-reciprocal re-ranking
#     original_dist = distance_matrix.cpu().numpy()

#     # Sort the distance matrix in ascending order
#     sorted_dist = np.argsort(original_dist, axis=1)

#     # Compute the k1 nearest neighbors for each gallery image
#     top_k = sorted_dist[:, :k1]

#     # Initialize the final distance matrix
#     final_dist = np.zeros_like(original_dist, dtype=np.float32)

#     # Perform re-ranking
#     for i in range(original_dist.shape[0]):
#         # Compute the k2 nearest neighbors among the k1 nearest neighbors
#         initial_rank = top_k[i]
#         temp_dist = original_dist[i]
#         neighbors = np.zeros_like(temp_dist, dtype=np.float32)
#         temp_dist_copy = temp_dist.copy()
#         temp_dist_copy[temp_dist_copy > lambda_value] = np.inf
#         neighbors[temp_dist_copy <= lambda_value] = temp_dist[temp_dist_copy <= lambda_value]

#         for j in range(1, k2):
#             if j < len(initial_rank) and initial_rank[j] < original_dist.shape[0]:
#                 temp_dist = original_dist[initial_rank[j]]
#                 neighbors2 = np.zeros_like(temp_dist, dtype=np.float32)
#                 temp_dist_copy = temp_dist.copy()
#                 temp_dist_copy[temp_dist_copy > lambda_value] = np.inf
#                 neighbors2[temp_dist_copy <= lambda_value] = temp_dist[temp_dist_copy <= lambda_value]
#                 neighbors += neighbors2

#         # Update the final distance matrix with re-ranked distances
#         final_dist[i] = neighbors

#     return final_dist


# def compute_k_reciprocal_neighbors(similarity_matrix, k1, k2):
#     # Compute the k-reciprocal neighbors
#     similarity_matrix = similarity_matrix.numpy()
#     k_reciprocal_matrix = np.zeros_like(similarity_matrix)

#     for i in range(similarity_matrix.shape[0]):
#         sample_indices = np.argsort(similarity_matrix[i])[::-1]
#         sample_indices = sample_indices[:k1 + k2]

#         for j in range(k1 + k2):
#             target_indices = np.argsort(similarity_matrix[:, sample_indices[j]])[::-1]
#             mutual_match_indices = np.where(target_indices == i)[0]
#             if mutual_match_indices.size > 0:
#                 k_reciprocal_matrix[i, sample_indices[j]] = mutual_match_indices[0]
#             else:
#                 k_reciprocal_matrix[i, sample_indices[j]] = -1

#     return k_reciprocal_matrix


# def re_rank(similarity_matrix, k_reciprocal_matrix, lambda_value):
#     # Perform re-ranking using k-reciprocal encoding
#     similarity_matrix = similarity_matrix.numpy()
#     k_reciprocal_matrix = k_reciprocal_matrix.astype(np.int64)
#     lambda_value = float(lambda_value)

#     for i in range(similarity_matrix.shape[0]):
#         common_neighbors = np.where(k_reciprocal_matrix[i] != -1)[0]

#         for j in range(similarity_matrix.shape[1]):
#             if j in common_neighbors:
#                 similarity_matrix[i, j] = (1 - lambda_value) * similarity_matrix[i, j] + lambda_value * similarity_matrix[k_reciprocal_matrix[i, j], j]
#             else:
#                 similarity_matrix[i, j] = similarity_matrix[i, j]

#         similarity_matrix[i] = similarity_matrix[i] / np.max(similarity_matrix[i])

#     return torch.from_numpy(similarity_matrix)

def extract_features(model, data_loader, print_freq=1, metric=None, norm=False):
    model.eval()
    batch_time = AverageMeter()
    data_time = AverageMeter()

    features = OrderedDict()
    labels = OrderedDict()

    end = time.time()
    for i, (imgs, fnames, pids, _) in enumerate(data_loader):
        data_time.update(time.time() - end)

        outputs = extract_cnn_feature(model, imgs, norm=norm)
        for fname, output, pid in zip(fnames, outputs, pids):
            features[fname] = output
            labels[fname] = pid

        batch_time.update(time.time() - end)
        end = time.time()

        if (i + 1) % print_freq == 0:
            print('Extract Features: [{}/{}]\t'
                  'Time {:.3f} ({:.3f})\t'
                  'Data {:.3f} ({:.3f})\t'
                  .format(i + 1, len(data_loader),
                          batch_time.val, batch_time.avg,
                          data_time.val, data_time.avg))

    return features, labels


def pairwise_distance(features, query=None, gallery=None, metric=None):
    useEuclidean = False

    if metric is None:
        useEuclidean = True

    if useEuclidean or metric.algorithm == "euclidean":
        if query is None and gallery is None:
            n = len(features)
            x = torch.cat(list(features.values()))
            x = x.view(n, -1)
            if metric is not None:
                x = metric.transform(x)
            dist = torch.pow(x, 2).sum(dim=1, keepdim=True) * 2
            dist = dist.expand(n, n) - 2 * torch.mm(x, x.t())
            return dist

        x = torch.cat([features[f].unsqueeze(0) for f, _, _ in query], 0)
        y = torch.cat([features[f].unsqueeze(0) for f, _, _ in gallery], 0)
        m, n = x.size(0), y.size(0)
        x = x.view(m, -1)
        y = y.view(n, -1)
        if metric is not None:
            x = metric.transform(x)
            y = metric.transform(y)
        dist = torch.pow(x, 2).sum(dim=1, keepdim=True).expand(m, n) + \
            torch.pow(y, 2).sum(dim=1, keepdim=True).expand(n, m).t()
        dist.addmm_(x, y.t(), beta=1, alpha=-2)
        return dist
    else:
        if query is None and gallery is None:
            n = len(features)
            x = torch.cat(list(features.values()))
            x = x.view(n, -1)
            if metric is not None:
                x = metric.transform(x)
            x_norm = x.norm(dim=1, keepdim=True)
            x_normalized = x.div(x_norm)
            dist = torch.mm(x_normalized, x_normalized.t())
            dist = 1 - dist
            return dist

        x = torch.cat([features[f].unsqueeze(0) for f, _, _ in query], 0)
        y = torch.cat([features[f].unsqueeze(0) for f, _, _ in gallery], 0)
        m, n = x.size(0), y.size(0)
        x = x.view(m, -1)
        y = y.view(n, -1)
        if metric is not None:
            x = metric.transform(x)
            y = metric.transform(y)
        x_norm = x.norm(dim=1, keepdim=True)
        y_norm = y.norm(dim=1, keepdim=True)
        x_normalized = x.div(x_norm)
        y_normalized = y.div(y_norm)
        dist = torch.mm(x_normalized, y_normalized.t())
        dist = 1 - dist
        return dist


def evaluate_all(distmat, dataset, query=None, gallery=None,
                 query_ids=None, gallery_ids=None,
                 query_cams=None, gallery_cams=None,
                 cmc_topk=(1, 5, 10)):
    
    if query is not None and gallery is not None:
        query_ids = [pid for _, pid, _ in query]
        gallery_ids = [pid for _, pid, _ in gallery]
        query_cams = [cam for _, _, cam in query]
        gallery_cams = [cam for _, _, cam in gallery]
    else:
        assert (query_ids is not None and gallery_ids is not None
                and query_cams is not None and gallery_cams is not None)

    # Compute mean AP
    mAP = mean_ap(distmat, query_ids, gallery_ids, query_cams, gallery_cams)
    print('Mean AP: {:4.1%}'.format(mAP))

    # Compute all kinds of CMC scores
    cmc_configs = {
        'allshots': dict(separate_camera_set=False, single_gallery_shot=False, first_match_break=False)}
    
    if dataset == 'market1501':
        cmc_configs['market1501'] = dict(separate_camera_set=False, single_gallery_shot=False, first_match_break=True)
    else:
        cmc_configs['dukemtmc'] = dict(separate_camera_set=False, single_gallery_shot=True, first_match_break=False)
    
    cmc_scores = {name: cmc(distmat, query_ids, gallery_ids,
                            query_cams, gallery_cams, **params)
                  for name, params in cmc_configs.items()}

    print('CMC Scores{:>12}'.format(dataset))
    for k in cmc_topk:
        print('  top-{:<4}{:12.1%}'.format(k, cmc_scores[dataset][k - 1]))

    # Use the 'dataset' cmc top-1 score for validation criterion
    return cmc_scores[dataset][0]


class Evaluator(object):
    def __init__(self, model):
        super(Evaluator, self).__init__()
        self.model = model

    def evaluate(self, data_loader, query, gallery, dataset, metric=None, norm=False, re_ranking=False):
        features, _ = extract_features(self.model, data_loader, norm=norm)
        distmat = pairwise_distance(features, query, gallery, metric=metric)

        if re_ranking:
            re_ranked_matrix = k_re_ranking(distmat)
            print(re_ranked_matrix)

            return evaluate_all(re_ranked_matrix, dataset=dataset, query=query, gallery=gallery)
            
        else:
            print(distmat)
            return evaluate_all(distmat, dataset=dataset, query=query, gallery=gallery)
