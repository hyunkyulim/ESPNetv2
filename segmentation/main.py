import loadData as ld
import os
import torch
import pickle
from cnn import SegmentationModel as net
from torch.autograd import Variable
import VisualizeGraph as viz
import torch.backends.cudnn as cudnn
import Transforms as myTransforms
import DataSet as myDataLoader
import time
from argparse import ArgumentParser
from IOUEval import iouEval
import torch.optim.lr_scheduler
import numpy as np

__author__ = "Sachin Mehta"

def val(args, val_loader, model, criterion):
    '''
    :param args: general arguments
    :param val_loader: loaded for validation dataset
    :param model: model
    :param criterion: loss function
    :return: average epoch loss, overall pixel-wise accuracy, per class accuracy, per class iu, and mIOU
    '''
    #switch to evaluation mode
    model.eval()

    iouEvalVal = iouEval(args.classes)

    epoch_loss = []

    total_batches = len(val_loader)
    for i, (input, target) in enumerate(val_loader):
        start_time = time.time()

        if args.onGPU:
            input = input.cuda(non_blocking=True) #torch.autograd.Variable(input, volatile=True)
            target = target.cuda(non_blocking=True)#torch.autograd.Variable(target, volatile=True)

        # run the mdoel
        output1 = model(input)

        # compute the loss
        loss = criterion(output1, target)

        epoch_loss.append(loss.item())

        time_taken = time.time() - start_time

        # compute the confusion matrix
        iouEvalVal.addBatch(output1.max(1)[1].data, target.data)

        print('[%d/%d] loss: %.3f time: %.2f' % (i, total_batches, loss.item(), time_taken))

    average_epoch_loss_val = sum(epoch_loss) / len(epoch_loss)

    overall_acc, per_class_acc, per_class_iu, mIOU = iouEvalVal.getMetric()

    return average_epoch_loss_val, overall_acc, per_class_acc, per_class_iu, mIOU

def train(args, train_loader, model, criterion, optimizer, epoch):
    '''
    :param args: general arguments
    :param train_loader: loaded for training dataset
    :param model: model
    :param criterion: loss function
    :param optimizer: optimization algo, such as ADAM or SGD
    :param epoch: epoch number
    :return: average epoch loss, overall pixel-wise accuracy, per class accuracy, per class iu, and mIOU
    '''
    # switch to train mode
    model.train()

    iouEvalTrain = iouEval(args.classes)

    epoch_loss = []

    total_batches = len(train_loader)
    for i, (input, target) in enumerate(train_loader):
        start_time = time.time()

        if args.onGPU:
            input = input.cuda(non_blocking=True) #torch.autograd.Variable(input, volatile=True)
            target = target.cuda(non_blocking=True)

        #run the mdoel
        output1, output2 = model(input)

        #set the grad to zero
        optimizer.zero_grad()
        loss1 = criterion(output1, target)
        loss2 = criterion(output2, target)
        loss = loss1 + loss2

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        epoch_loss.append(loss.item())
        time_taken = time.time() - start_time

        #compute the confusion matrix
        iouEvalTrain.addBatch(output1.max(1)[1].data, target.data)

        print('[%d/%d] loss: %.3f time:%.2f' % (i, total_batches, loss.item(), time_taken))

    average_epoch_loss_train = sum(epoch_loss) / len(epoch_loss)

    overall_acc, per_class_acc, per_class_iu, mIOU = iouEvalTrain.getMetric()

    return average_epoch_loss_train, overall_acc, per_class_acc, per_class_iu, mIOU

def save_checkpoint(state, filenameCheckpoint='checkpoint.pth.tar'):
    '''
    helper function to save the checkpoint
    :param state: model state
    :param filenameCheckpoint: where to save the checkpoint
    :return: nothing
    '''
    torch.save(state, filenameCheckpoint)

def netParams(model):
    '''
    helper function to see total network parameters
    :param model: model
    :return: total network parameters
    '''
    return np.sum([np.prod(parameter.size()) for parameter in model.parameters()])

def trainValidateSegmentation(args):
    '''
    Main function for trainign and validation
    :param args: global arguments
    :return: None
    '''


    # load the model
    cuda_available = torch.cuda.is_available()
    num_gpus = torch.cuda.device_count()
    model = net.EESPNet_Seg(args.classes, s=args.s, pretrained=args.pretrained, gpus=num_gpus)

    if num_gpus >= 1:
        model = torch.nn.DataParallel(model)

    args.savedir = args.savedir + str(args.s) + '/'

    # create the directory if not exist
    if not os.path.exists(args.savedir):
        os.mkdir(args.savedir)

    if args.visualizeNet:
        x = torch.randn(1, 3, args.inWidth, args.inHeight)

        if cuda_available:
            x = x.cuda()
            model = model.cuda()

        y, _ = model.forward(x)
        g = viz.make_dot(y)
        g.render(args.savedir + 'model.png', view=False)
    # check if processed data file exists or not
    if not os.path.isfile(args.cached_data_file):
        dataLoad = ld.LoadData(args.data_dir, args.classes, args.cached_data_file)
        data = dataLoad.processData()
        if data is None:
            print('Error while pickling data. Please check.')
            exit(-1)
    else:
        data = pickle.load(open(args.cached_data_file, "rb"))



    if cuda_available:
        args.onGPU = True
        model = model.cuda()

    total_paramters = netParams(model)
    print('Total network parameters: ' + str(total_paramters))

    # define optimization criteria
    weight = torch.from_numpy(data['classWeights']) # convert the numpy array to torch
    if args.onGPU:
        weight = weight.cuda()

    criteria = torch.nn.CrossEntropyLoss(weight) #weight

    if args.onGPU:
        criteria = criteria.cuda()

    print('Data statistics')
    print(data['mean'], data['std'])
    print(data['classWeights'])

    #compose the data with transforms
    trainDataset_main = myTransforms.Compose([
        myTransforms.Normalize(mean=data['mean'], std=data['std']),
        myTransforms.RandomCropResize(size=(args.inWidth, args.inHeight)),
        myTransforms.RandomFlip(),
        #myTransforms.RandomCrop(64).
        myTransforms.ToTensor(args.scaleIn),
        #
    ])

    trainDataset_scale1 = myTransforms.Compose([
        myTransforms.Normalize(mean=data['mean'], std=data['std']),
        myTransforms.RandomCropResize(size=(int(args.inWidth*1.5), int(1.5*args.inHeight))),
        myTransforms.RandomFlip(),
        myTransforms.ToTensor(args.scaleIn),
        #
    ])

    trainDataset_scale2 = myTransforms.Compose([
        myTransforms.Normalize(mean=data['mean'], std=data['std']),
        myTransforms.RandomCropResize(size=(int(args.inWidth*1.25), int(1.25*args.inHeight))), # 1536, 768
        myTransforms.RandomFlip(),
        myTransforms.ToTensor(args.scaleIn),
        #
    ])

    trainDataset_scale3 = myTransforms.Compose([
        myTransforms.Normalize(mean=data['mean'], std=data['std']),
        myTransforms.RandomCropResize(size=(int(args.inWidth*0.75), int(0.75*args.inHeight))),
        myTransforms.RandomFlip(),
        myTransforms.ToTensor(args.scaleIn),
        #
    ])

    trainDataset_scale4 = myTransforms.Compose([
        myTransforms.Normalize(mean=data['mean'], std=data['std']),
        myTransforms.RandomCropResize(size=(int(args.inWidth*0.5), int(0.5*args.inHeight))),
        myTransforms.RandomFlip(),
        myTransforms.ToTensor(args.scaleIn),
        #
    ])


    valDataset = myTransforms.Compose([
        myTransforms.Normalize(mean=data['mean'], std=data['std']),
        myTransforms.Scale(1024, 512),
        myTransforms.ToTensor(args.scaleIn),
        #
    ])

    # since we training from scratch, we create data loaders at different scales
    # so that we can generate more augmented data and prevent the network from overfitting

    trainLoader = torch.utils.data.DataLoader(
        myDataLoader.MyDataset(data['trainIm'], data['trainAnnot'], transform=trainDataset_main),
        batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers, pin_memory=True)

    trainLoader_scale1 = torch.utils.data.DataLoader(
        myDataLoader.MyDataset(data['trainIm'], data['trainAnnot'], transform=trainDataset_scale1),
        batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers, pin_memory=True)

    trainLoader_scale2 = torch.utils.data.DataLoader(
        myDataLoader.MyDataset(data['trainIm'], data['trainAnnot'], transform=trainDataset_scale2),
        batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers, pin_memory=True)

    trainLoader_scale3 = torch.utils.data.DataLoader(
        myDataLoader.MyDataset(data['trainIm'], data['trainAnnot'], transform=trainDataset_scale3),
        batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers, pin_memory=True)

    trainLoader_scale4 = torch.utils.data.DataLoader(
        myDataLoader.MyDataset(data['trainIm'], data['trainAnnot'], transform=trainDataset_scale4),
        batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers, pin_memory=True)

    valLoader = torch.utils.data.DataLoader(
        myDataLoader.MyDataset(data['valIm'], data['valAnnot'], transform=valDataset),
        batch_size=args.batch_size, shuffle=False, num_workers=args.num_workers, pin_memory=True)

    if args.onGPU:
        cudnn.benchmark = True

    start_epoch = 0
    best_val = 0
    lr = args.lr

    optimizer = torch.optim.Adam(model.parameters(), lr, (0.9, 0.999), eps=1e-08, weight_decay=5e-4)
    # we step the loss by 2 after step size is reached
    #scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=args.step_loss, gamma=0.5)



    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            start_epoch = checkpoint['epoch']
            best_val = checkpoint['best_val']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})"
                .format(args.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))


    logFileLoc = args.savedir + args.logFile
    if os.path.isfile(logFileLoc):
        logger = open(logFileLoc, 'a')
    else:
        logger = open(logFileLoc, 'w')
        logger.write("Parameters: %s" % (str(total_paramters)))
        logger.write("\n%s\t%s\t%s\t%s\t%s\t" % ('Epoch', 'Loss(Tr)', 'Loss(val)', 'mIOU (tr)', 'mIOU (val'))
    logger.flush()



    for epoch in range(start_epoch, args.max_epochs):

        #scheduler.step(epoch)
        poly_lr_scheduler(args, optimizer, epoch)
        lr = 0
        for param_group in optimizer.param_groups:
            lr = param_group['lr']
        print("Learning rate: " +  str(lr))

        # train for one epoch
        # We consider 1 epoch with all the training data (at different scales)
        train(args, trainLoader_scale1, model, criteria, optimizer, epoch)
        train(args, trainLoader_scale2, model, criteria, optimizer, epoch)
        train(args, trainLoader_scale4, model, criteria, optimizer, epoch)
        train(args, trainLoader_scale3, model, criteria, optimizer, epoch)
        lossTr, overall_acc_tr, per_class_acc_tr, per_class_iu_tr, mIOU_tr = train(args, trainLoader, model, criteria, optimizer, epoch)

        # evaluate on validation set
        lossVal, overall_acc_val, per_class_acc_val, per_class_iu_val, mIOU_val = val(args, valLoader, model, criteria)

        is_best = mIOU_val > best_val
        save_checkpoint({
            'epoch': epoch + 1,
            'state_dict': model.state_dict(),
            'optimizer': optimizer.state_dict(),
            'lr': lr,
            'best_val': best_val,
        }, args.savedir + 'checkpoint.pth.tar')

        #save the model also
        if is_best:
            model_file_name = args.savedir + '/model_best.pth'
            torch.save(model.state_dict(), model_file_name)

        with open(args.savedir + 'acc_' + str(epoch) + '.txt', 'w') as log:
            log.write("\nEpoch: %d\t Overall Acc (Tr): %.4f\t Overall Acc (Val): %.4f\t mIOU (Tr): %.4f\t mIOU (Val): %.4f" % (epoch, overall_acc_tr, overall_acc_val, mIOU_tr, mIOU_val))
            log.write('\n')
            log.write('Per Class Training Acc: ' + str(per_class_acc_tr))
            log.write('\n')
            log.write('Per Class Validation Acc: ' + str(per_class_acc_val))
            log.write('\n')
            log.write('Per Class Training mIOU: ' + str(per_class_iu_tr))
            log.write('\n')
            log.write('Per Class Validation mIOU: ' + str(per_class_iu_val))

        logger.write("\n%d\t\t%.4f\t\t%.4f\t\t%.4f\t\t%.4f\t\t%.7f" % (epoch, lossTr, lossVal, mIOU_tr, mIOU_val, lr))
        logger.flush()
        print("Epoch : " + str(epoch) + ' Details')
        print("\nEpoch No.: %d\tTrain Loss = %.4f\tVal Loss = %.4f\t mIOU(tr) = %.4f\t mIOU(val) = %.4f" % (epoch, lossTr, lossVal, mIOU_tr, mIOU_val))
    logger.close()

def poly_lr_scheduler(args, optimizer, epoch, power=0.9):
        lr = round(args.lr*(1 - epoch/args.max_epochs)**power, 8)
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr

        return lr


if __name__ == '__main__':

    parser = ArgumentParser()
    parser.add_argument('--model', default="ESPNetv2", help='Model name')
    parser.add_argument('--data_dir', default="./city", help='Data directory')
    parser.add_argument('--inWidth', type=int, default=1024, help='Width of RGB image')
    parser.add_argument('--inHeight', type=int, default=512, help='Height of RGB image')
    parser.add_argument('--scaleIn', type=int, default=1, help='For ESPNet-C, scaleIn=8. For ESPNet, scaleIn=1')
    parser.add_argument('--max_epochs', type=int, default=300, help='Max. number of epochs')
    parser.add_argument('--num_workers', type=int, default=12, help='No. of parallel threads')
    parser.add_argument('--batch_size', type=int, default=10, help='Batch size. 12 for ESPNet-C and 6 for ESPNet. '
                                                                   'Change as per the GPU memory')
    parser.add_argument('--step_loss', type=int, default=100, help='Decrease learning rate after how many epochs.')
    parser.add_argument('--lr', type=float, default=5e-4, help='Initial learning rate')
    parser.add_argument('--savedir', default='./results_espnetv2_', help='directory to save the results')
    parser.add_argument('--visualizeNet', type=bool, default=False, help='If you want to visualize the model structure')
    parser.add_argument('--resume', type=str, default='', help='Use this flag to load last checkpoint for training')  #
    parser.add_argument('--classes', type=int, default=20, help='No of classes in the dataset. 20 for cityscapes')
    parser.add_argument('--cached_data_file', default='city.p', help='Cached file name')
    parser.add_argument('--logFile', default='trainValLog.txt', help='File that stores the training and validation logs')
    parser.add_argument('--pretrained', default='../imagenet/pretrained_weights/espnetv2_s_1.0.pth', help='Pretrained ESPNetv2 weights.')
    parser.add_argument('--s', default=1, type=float, help='scaling parameter')

    trainValidateSegmentation(parser.parse_args())

