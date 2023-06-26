from __future__ import print_function, absolute_import

from collections import OrderedDict
import numpy as np
import torch
import time

from .feature_extraction import extract_cnn_feature
from .evaluation_metrics import cmc, mean_ap
from .utils.meters import AverageMeter

def k_reciprocal_re_ranking(queryFeat, galleryFeat, k1=20, k2=6, lambda_value=0.3):
    
    query_num = queryFeat.size(0)
    all_num = query_num + galleryFeat.size(0)

    feat = torch.cat([queryFeat,galleryFeat])
    distmat = torch.pow(feat,2).sum(dim=1, keepdim=True).expand(all_num,all_num) + torch.pow(feat, 2).sum(dim=1, keepdim=True).expand(all_num, all_num).t()
    distmat.addmm_(1,-2,feat,feat.t())
    original_dist = distmat.cpu().numpy()
    del feat

    gallery_num = original_dist.shape[0]
    original_dist = np.transpose(original_dist / np.max(original_dist, axis=0))
    V = np.zeros_like(original_dist).astype(np.float16)
    initial_rank = np.argsort(original_dist).astype(np.int32)

    print('Starting re-ranking')
    for i in range(all_num):
        # k-reciprocal neighbors
        forward_k_neigh_index = initial_rank[i, :k1 + 1]
        backward_k_neigh_index = initial_rank[forward_k_neigh_index, :k1 + 1]
        fi = np.where(backward_k_neigh_index == i)[0]
        k_reciprocal_index = forward_k_neigh_index[fi]
        k_reciprocal_expansion_index = k_reciprocal_index

        for j in range(len(k_reciprocal_index)):
            candidate = k_reciprocal_index[j]
            candidate_forward_k_neigh_index = initial_rank[candidate, :int(np.around(k1 / 2)) + 1]
            candidate_backward_k_neigh_index = initial_rank[candidate_forward_k_neigh_index, :int(np.around(k1 / 2)) + 1]
            fi_candidate = np.where(candidate_backward_k_neigh_index == candidate)[0]
            candidate_k_reciprocal_index = candidate_forward_k_neigh_index[fi_candidate]

            if len(np.intersect1d(candidate_k_reciprocal_index, k_reciprocal_index)) > 2 / 3 * len(candidate_k_reciprocal_index):
                k_reciprocal_expansion_index = np.append(k_reciprocal_expansion_index, candidate_k_reciprocal_index)

        k_reciprocal_expansion_index = np.unique(k_reciprocal_expansion_index)
        weight = np.exp(-original_dist[i, k_reciprocal_expansion_index])
        V[i, k_reciprocal_expansion_index] = weight / np.sum(weight)

    original_dist = original_dist[:query_num, ]

    if k2 != 1:
        V_qe = np.zeros_like(V, dtype=np.float16)

        for i in range(all_num):
            V_qe[i, :] = np.mean(V[initial_rank[i, :k2], :], axis=0)

        V = V_qe
        del V_qe

    del initial_rank
    invIndex = []

    for i in range(gallery_num):
        invIndex.append(np.where(V[:, i] != 0)[0])

    jaccard_dist = np.zeros_like(original_dist, dtype=np.float16)

    for i in range(query_num):
        temp_min = np.zeros(shape=[1, gallery_num], dtype=np.float16)
        indNonZero = np.where(V[i, :] != 0)[0]
        indImages = [invIndex[ind] for ind in indNonZero]

        for j in range(len(indNonZero)):
            temp_min[0, indImages[j]] = temp_min[0, indImages[j]] + np.minimum(V[i, indNonZero[j]], V[indImages[j], indNonZero[j]])

        jaccard_dist[i] = 1 - temp_min / (2 - temp_min)

    final_dist = jaccard_dist * (1 - lambda_value) + original_dist * lambda_value

    del original_dist
    del V
    del jaccard_dist

    final_dist = final_dist[:query_num, query_num:]

    return final_dist

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
        return dist, x, y


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
        print('  rank-{:<4}{:12.1%}'.format(k, cmc_scores[dataset][k - 1]))

    # Use the 'dataset' cmc top-1 score for validation criterion
    return cmc_scores[dataset][0]


class Evaluator(object):
    def __init__(self, model):
        super(Evaluator, self).__init__()
        self.model = model

    def evaluate(self, data_loader, query, gallery, dataset, metric=None, norm=False, re_ranking=False):
        features, _ = extract_features(self.model, data_loader, norm=norm)
        distmat, queryFeat, galleryFeat = pairwise_distance(features, query, gallery, metric=metric)

        if re_ranking:
            final_ranking = k_reciprocal_re_ranking(queryFeat, galleryFeat)
            return evaluate_all(final_ranking, dataset=dataset, query=query, gallery=gallery)
            
        else:
            return evaluate_all(distmat, dataset=dataset, query=query, gallery=gallery)
