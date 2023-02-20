from __future__ import print_function, absolute_import
import argparse
import os.path as osp

import numpy as np
import sys
import torch
from torch import nn
from torch.backends import cudnn
from torch.utils.data import DataLoader

from reid import datasets
from reid import models
from reid.dist_metric import DistanceMetric
from reid.trainers import Trainer
from reid.evaluators import Evaluator
from reid.utils.data import transforms as T
from reid.utils.data.preprocessor import Preprocessor
from reid.utils.logging import Logger
from reid.utils.serialization import load_checkpoint, save_checkpoint

# set and return all the needed elements for the model (dataset, num_classes, train_loader, val_loader, test_loader)
def get_data(name, split_id, data_dir, height, width, batch_size, workers, combine_trainval):

    # get the root folder
    root = osp.join(data_dir, name)

    # create a dataset instance
    dataset = datasets.create(name, root, split_id=split_id)

    # set the normalization parameters (cropping and scale factors)
    normalizer = T.Normalize(mean=[0.485, 0.456, 0.406],
                             std=[0.229, 0.224, 0.225])

    # set the training set and teh number of classes
    train_set = dataset.trainval if combine_trainval else dataset.train
    num_classes = (dataset.num_trainval_ids if combine_trainval else dataset.num_train_ids)

    # compose several transforms together
    train_transformer = T.Compose([
        T.RandomSizedRectCrop(height, width),
        T.RandomHorizontalFlip(),
        T.ToTensor(),
        normalizer,
    ])

    test_transformer = T.Compose([
        T.RectScale(height, width),
        T.ToTensor(),
        normalizer,
    ])


    train_loader = DataLoader(
        Preprocessor(train_set, root=dataset.images_dir,
                     transform=train_transformer),
        batch_size=batch_size, num_workers=workers,
        shuffle=True, pin_memory=True, drop_last=True)

    val_loader = DataLoader(
        Preprocessor(dataset.val, root=dataset.images_dir,
                     transform=test_transformer),
        batch_size=batch_size, num_workers=workers,
        shuffle=False, pin_memory=True)

    test_loader = DataLoader(
        Preprocessor(list(set(dataset.query) | set(dataset.gallery)),
                     root=dataset.images_dir, transform=test_transformer),
        batch_size=batch_size, num_workers=workers,
        shuffle=False, pin_memory=True)

    return dataset, num_classes, train_loader, val_loader, test_loader


def main(args):

    # generate pseudo-random number based on the seed (the seed can be every number)
    np.random.seed(args.seed)

    # set the seed for generating random numbers
    torch.manual_seed(args.seed)
    
    # enable the buildin auto-tuner in order to find the best algorithm to use
    cudnn.benchmark = True

    # redirect print to both console and log file
    if not args.evaluate:
        sys.stdout = Logger(osp.join(args.logs_dir, 'log.txt'))

    # create data loaders
    if args.height is None or args.width is None:
        args.height, args.width = (144, 56) if args.arch == 'inception' else (256, 128)
    
        # retrieve the dataset, the number of classes, the train, valuation and test loaders
    dataset, num_classes, train_loader, val_loader, test_loader = get_data(args.dataset, args.split, args.data_dir, args.height, args.width, args.batch_size, args.workers, args.combine_trainval)

    # create model with the specified parameters
    model = models.create(args.arch, num_features=args.features, dropout=args.dropout, num_classes=num_classes)

    # if training was interrupted before the finish, it resumes by loading the checkpoint (saved in a specific file)
    start_epoch = best_top1 = 0
    if args.resume:
        checkpoint = load_checkpoint(args.resume)
        model.load_state_dict(checkpoint['state_dict'])
        start_epoch = checkpoint['epoch']
        best_top1 = checkpoint['best_top1']
        print("=> Start epoch {}  best top1 {:.1%}".format(start_epoch, best_top1))

    # parallelize the execution of the given module by splitting the input across the specified devices
    # enabling GPU acceleration on Mac devices
    if torch.backends.mps.is_available():
        mps_device = torch.device("mps")
        model = nn.DataParallel(model).to(mps_device)
    else:
        model = nn.DataParallel(model).cuda()

    # Distance metric
    metric = DistanceMetric(algorithm=args.dist_metric)

    # Evaluator
    evaluator = Evaluator(model)
    if args.evaluate:
        metric.train(model, train_loader)
        print("Validation:")
        evaluator.evaluate(val_loader, dataset.val, dataset.val, metric)
        print("Test:")
        evaluator.evaluate(test_loader, dataset.query, dataset.gallery, metric)
        return

    # Criterion
    # choice of the loss function
    # enabling GPU acceleration on Mac devices
    if torch.backends.mps.is_available():
        mps_device = torch.device("mps")
        criterion = nn.CrossEntropyLoss().to(mps_device)
    else:
        criterion = nn.CrossEntropyLoss().cuda()

    # Optimizer
    # performe a parameter optimization with Stochastic Gradient Descent
    if hasattr(model.module, 'base'):
        base_param_ids = set(map(id, model.module.base.parameters()))
        new_params = [p for p in model.parameters() if
                      id(p) not in base_param_ids]
        param_groups = [
            {'params': model.module.base.parameters(), 'lr_mult': 0.1},
            {'params': new_params, 'lr_mult': 1.0}]
    else:
        param_groups = model.parameters()
    optimizer = torch.optim.SGD(param_groups, lr=args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay,
                                nesterov=True)

    # Trainer
    trainer = Trainer(model, criterion)

    # Schedule learning rate
    # adjust the learning rate based on the number of epochs
    def adjust_lr(epoch):
        step_size = 60 if args.arch == 'inception' else 40
        lr = args.lr * (0.1 ** (epoch // step_size))
        for g in optimizer.param_groups:
            g['lr'] = lr * g.get('lr_mult', 1)

    # Start training
    for epoch in range(start_epoch, args.epochs):
        adjust_lr(epoch)
        trainer.train(epoch, train_loader, optimizer)
        if epoch < args.start_save:
            continue
        top1 = evaluator.evaluate(val_loader, dataset.val, dataset.val)

        is_best = top1 > best_top1
        best_top1 = max(top1, best_top1)
        save_checkpoint({
            'state_dict': model.module.state_dict(),
            'epoch': epoch + 1,
            'best_top1': best_top1,
        }, is_best, fpath=osp.join(args.logs_dir, 'checkpoint.pth.tar'))

        print('\n * Finished epoch {:3d}  top1: {:5.1%}  best: {:5.1%}{}\n'.
              format(epoch, top1, best_top1, ' *' if is_best else ''))

    # Final test
    # perform an ultimate test with the best model found
    print('Test with best model:')
    checkpoint = load_checkpoint(osp.join(args.logs_dir, 'model_best.pth.tar'))
    model.module.load_state_dict(checkpoint['state_dict'])
    metric.train(model, train_loader)
    evaluator.evaluate(test_loader, dataset.query, dataset.gallery, metric)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Softmax loss classification")
    # data
        # datasets.names() = ['viper', 'cuhk01', 'cuhk03', 'market1501', 'dukemtmc']
    parser.add_argument('-d', '--dataset', type=str, default='cuhk03', choices=datasets.names())

        # bach-size = number of samples (images) that will be propagated through the network every epoch
    parser.add_argument('-b', '--batch-size', type=int, default=256)

        # workers = number of subprocess to use for data loading
    parser.add_argument('-j', '--workers', type=int, default=4)

        # split = number of chunks in which to divide the tensor
    parser.add_argument('--split', type=int, default=0)

        # height, width = input dimension
    parser.add_argument('--height', type=int, help="input height, default: 256 for resnet*, " "144 for inception")
    parser.add_argument('--width', type=int, help="input width, default: 128 for resnet*, " "56 for inception")

        # training set and test set are used both for training
    parser.add_argument('--combine-trainval', action='store_true', help="train and val sets together for training, " "val set alone for validation")
    
    # model
        # models.names() = ['inception', 'resnet18', 'resnet34', 'resnet50', 'resnet101', 'resnet152']
    parser.add_argument('-a', '--arch', type=str, default='resnet50', choices=models.names())

        # features = number of features to use
    parser.add_argument('--features', type=int, default=128)

        # dropout = probability of dropping the connections between nodes in order to avoid overfitting
    parser.add_argument('--dropout', type=float, default=0.5)
   
    # optimizer
        # lr = learning rate
    parser.add_argument('--lr', type=float, default=0.1, help="learning rate of new parameters, for pretrained " "parameters it is 10 times smaller than this")

        # momentum = variant of the stochastic gradient descent that speed up learning and avoid getting stuck in local minima
    parser.add_argument('--momentum', type=float, default=0.9)

        # weight-decay = number that is multiplied to the sum of the squares of all the weights in the network to reduce complexity
    parser.add_argument('--weight-decay', type=float, default=5e-4)

    # training configs
    parser.add_argument('--resume', type=str, default='', metavar='PATH')
    parser.add_argument('--evaluate', action='store_true', help="evaluation only")

        # epochs = number of iterations for the network
    parser.add_argument('--epochs', type=int, default=50)

        # start_save = epoch at which to start saving the model
    parser.add_argument('--start_save', type=int, default=0, help="start saving checkpoints after specific epoch")

        #seed = number used to randomly generate values to assign to the weights of the network
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--print-freq', type=int, default=1)

    # metric learning
        # dist-metric = distance to learn in a low dimensional space such that similar images in the input space have a lower distance compared to dissimilar images (that will have a higher distance)
    parser.add_argument('--dist-metric', type=str, default='euclidean', choices=['euclidean', 'kissme'])
    
    # misc
        # set working directory
    working_dir = osp.dirname(osp.abspath(__file__))

        # data-dir = where to save data (such as the datasets)
    parser.add_argument('--data-dir', type=str, metavar='PATH', default=osp.join(working_dir, 'data'))

        # logs-dir = where to save logs files
    parser.add_argument('--logs-dir', type=str, metavar='PATH', default=osp.join(working_dir, 'logs'))
    
    main(parser.parse_args())
