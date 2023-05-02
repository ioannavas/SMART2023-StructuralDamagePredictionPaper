#! /usr/bin/env python
from structureDamagePrediction.datahandling import StructuralDamageDataAndMetadataReader, StructuralDamageDataset
from datetime import datetime
from structureDamagePrediction.utils import StartEndLogger
import numpy as np
import structureDamagePrediction.models as models
from structureDamagePrediction.training import NeuralNetTrainer
from torch.utils.data import DataLoader
import torch, math, random
from sklearn.model_selection import train_test_split
import scipy.stats as stats
import sys
import argparse

def main():
    # Setup reproducibility
    torch.manual_seed = 100
    random.seed(100)
    np.random.seed(100)

    # Set up reproducibility for data splitting
    def seed_worker(worker_id):
        worker_seed = torch.initial_seed() % 2**32
        np.random.seed(worker_seed)
        random.seed(worker_seed)
    torch_gen = torch.Generator()
    torch_gen.manual_seed(0)    

    # Init utils
    l = StartEndLogger()    
    # Init argument parser
    parser = argparse.ArgumentParser(description='Structural data analysis and prediction.')

    parser.add_argument("-b", "--baseDir",  help="The base directory of the dataset. (Default: data/)", default="data/")

    LEAVE_ONE_OUT = "leave-one-out"
    STRATIFY = "stratify"
    RANDOM = "random"
    parser.add_argument("-s", "--splittingMethod", help="The splitting method: leave-one-out or stratify or random. (Default: leave-one-out)",
                        choices=[LEAVE_ONE_OUT, STRATIFY, RANDOM],
                        default=LEAVE_ONE_OUT)

    CLASSIFICATION = "classification"
    REGRESSION = "regression"
    parser.add_argument("-p", "--predictionType", help="Type of prediction: classification or regression. (Default: classification)", 
                        choices=[CLASSIFICATION, REGRESSION], default=CLASSIFICATION)

    parser.add_argument("-e", "--epochs", help="Epochs for neural net training. (Default: 500)", 
                        type=int, default=1000)
    
    SEQUENCE = 'sequence'
    FOURIER = 'fourier'
    parser.add_argument("-r", "--representation", help="Type of representation: sequence or fourier. (Default: sequence)", 
                    choices=[SEQUENCE, FOURIER], default=SEQUENCE)
    
    parser.add_argument("-fn", "--fourierDimensions", type=int, help="Fourier dimensions to keep. (Default: 3)", default=3)

    LSTM = 'lstm'
    LINEAR = 'linear'
    KNN = 'knn'
    DUMMY = 'dummy'
    parser.add_argument("-cl", "--classifier", choices=[LSTM,LINEAR,KNN,DUMMY], help="Selected classifier. (Default: KNN)", default=KNN)
    # TODO: Add arguments for metadata field access as label
    # TODO: Add arguments for patience

    # Read arguments
    args = parser.parse_args(sys.argv[1:])
    base_dir = args.baseDir
    splitting = args.splittingMethod
    classification = args.predictionType == CLASSIFICATION
    classifier = args.classifier
    n_epochs = args.epochs
    fourier_dims = args.fourierDimensions
    representation = args.representation

    # Select feature vector (sequence) transform function
    if representation == FOURIER:
        # Init transformation function
        def multidim_fft_transform(seq, dimensions=3, fourier_dimensions=fourier_dims):
            # Init concat
            ffts = []
            # For each dimension of the sequence
            for iDim in range(dimensions):
                # Get the data
                cur_dim_seq = seq[:, iDim]
                # Extract fourier
                spectrum = torch.fft.fft(cur_dim_seq, n=fourier_dimensions)
                # Store for concatenation
                ffts.append(spectrum.real)

            # Concatenate and return
            res = torch.cat(ffts)
            return res
        sequence_transform = multidim_fft_transform        
    else:
        sequence_transform = None
    l.log("Programme arguments:\n%s"%(str(args)))



    # Init reader
    reader = StructuralDamageDataAndMetadataReader(base_dir=base_dir)
    # Read data and metadata
    data, meta_data = reader.read_data_and_metadata()

    # Transformation function for classification
    def transform_func(x):
        idx = [0.025, 0.05, 0.10].index(x)
        return idx

    # Regression (no change)    
    # transform_func = None

    # Meta-data format
    # case_id, dmg_perc, dmg_tensor, dmg_loc_x, dmg_loc_y    
    dataset = StructuralDamageDataset(data, meta_data, 
                                      tgt_tuple_index_in_metadata=1,  tgt_row_in_metadata=None, tgt_col_in_metadata=None, # What to use: dmg percentage
                                      label_transform_func=transform_func, feature_vector_transform_func=sequence_transform)


    # Update booleans
    leave_one_out = splitting == LEAVE_ONE_OUT
    stratify = splitting == STRATIFY


    if leave_one_out:
        number_of_runs = len(dataset)
    else:
        number_of_runs = 3

    predicted_list = []
    real_list = []

    for iRun in range(number_of_runs):
        l.log("+++++ Starting run #%d"%(iRun))

        if leave_one_out:
            test_perc = 1.0 / len(dataset)
        else:
            test_perc = 0.20

        if stratify:
            _, test_instance_idx = train_test_split(np.arange(len(dataset)),
                                                        test_size=test_perc,
                                                        random_state=5, # Reproducibility
                                                        shuffle=True,
                                                        stratify=list(dataset.labels())
                                                        )
        else:
            # Choose as test instance index the current run number
            if leave_one_out:
                test_instance_idx = [iRun]
            else:
                # Choose test instance indexes
                test_instance_idx=np.random.choice(list(range(0, len(dataset))),  size = math.ceil(test_perc * len(dataset)), replace=False)
                l.log("Selected instances: %s"%(str(test_instance_idx)))

        # Create train and test data
        train_data = []
        test_data = []
        for idx,entry in enumerate(dataset):
            if idx in test_instance_idx:
                test_data.append(entry)
            else:
                train_data.append(entry)

        l.log("Train / test sizes: %4d /%4d"%(len(train_data), len(test_data)))
        
        train_dataloader = DataLoader(train_data, batch_size=4, shuffle=True, worker_init_fn=seed_worker, generator=torch_gen)
        test_dataloader = DataLoader(test_data, batch_size=1, shuffle=False)

        # Train model
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        l.log("Device for learning: %s"%(device.type))


        
        if classification:
            modelsLossesAndSupportedRepr={
                # Non-NN
                LSTM: (models.LSTMClassificationModel(device=device, num_classes=3),torch.nn.CrossEntropyLoss(), [SEQUENCE]),
                LINEAR: (models.SimpleLinear(device=device,num_classes=3), [FOURIER]),
                # NN
                KNN: (models.KNNModel(3, 3 * fourier_dims), None, [FOURIER]),
                DUMMY: (models.DummyModel(), None, [FOURIER]),
            }

            # Classification
            #################
            # Neural netowrk models
            model, loss_fn, compat_repr = modelsLossesAndSupportedRepr[classifier]
            if representation not in compat_repr:
                raise RuntimeError("Model %s only supports representations: %s."%(classifier,str(compat_repr)))
        else:
            # Regression
            #############
            model = models.LSTMRegressionModel(device=device)
            # model = models.RNNModel(device=device)
            loss_fn=torch.nn.L1Loss()

        if not isinstance(model, models.SKLearnModel):
            trainer = NeuralNetTrainer(model, 
                            optimizer=torch.optim.Adam(params=model.parameters(), 
                                                                betas=(0.9, 0.999), eps=10e-7, lr=1e-4) , 
                            # optimizer=torch.optim.SGD(model.parameters(),lr=0.1, momentum=0.1),
                            n_epochs=n_epochs, device=device, loss_fn=loss_fn)
            trainer.train(train_dataloader,min_abs_loss_change=0.0001, patience_epochs=200, sufficient_loss=0.001, output_every=100)
            final_model = trainer.get_model()
        else:
            all_X = []
            all_y = []
            # Gather batches
            for X_batch, y_batch in train_dataloader:
                all_X.append(X_batch)
                all_y.append(y_batch)

            all_X = torch.cat(all_X)
            all_y = torch.cat(all_y)
            model.fit(all_X, all_y)
            final_model = model

        l.start("Validation...")
        if not isinstance(model, models.SKLearnModel):
            final_model.eval()
        with torch.no_grad():
            for X_test, y_test in test_dataloader:
                X_test = X_test.to(device)
                y_test = y_test.to(device)
                real_list.append(y_test.item())

                y_pred = final_model(X_test)
                if not isinstance(model, models.SKLearnModel):
                    test_loss = trainer.loss_fn(y_pred, y_test).cpu()
                else:
                    test_loss = abs(y_pred.cpu() - y_test.cpu())

                if classification:
                    if not isinstance(model, models.SKLearnModel):                    
                        ypred_final = y_pred.max(1).indices
                    else:
                        ypred_final = y_pred.cpu()

                    predicted_list.append(ypred_final.cpu().item())
                    prc_loss = 0.0
                else:
                    ypred_final = y_pred.item()
                    predicted_list.append(ypred_final.cpu().item())
                    prc_loss = 100 * test_loss / y_test

                l.log("True: %8.6f -- Predicted: %8.6f (Loss: %8.6f; Percantile: %5.2f%%)"%(y_test.cpu().item(), ypred_final, test_loss ,prc_loss))

        l.end()

    l.log("Outputting overall results list:\nReal,Predicted")
    l.log("\n".join(map(lambda x: "%d,%d"%(x[0],x[1]),list(zip(real_list, predicted_list)))), no_date=True)

    if classification:
        # TODO: Break into smaller functions

        # Accuracy
        accuracy = 1.0 * sum([real_list[iCnt] == predicted_list[iCnt] for iCnt in range(len(real_list))]) / len(real_list)
        l.log("Accuracy: %6.4f"%(accuracy))

        def acc(realAndPredTupleList):
            if realAndPredTupleList[0] == realAndPredTupleList[1]:
                return 1.0
            else:
                return 0.0
        
        # Mean and std error
        perFoldAcc = list(map(acc, zip(real_list,predicted_list)))
        avgAcc = np.average(perFoldAcc)
        stdErrAcc = np.std(perFoldAcc)/ np.sqrt(len(real_list))
        l.log("Avg accuracy  %6.4f+/- stderr %6.4f"%(avgAcc, stdErrAcc))

        # Confusion matrix
        classes = list(map(str, set(real_list)))
        conf_matrix = dict()
        # Init
        for curClassTrue in classes:
            conf_matrix[curClassTrue] = dict()
            for curClassPred in classes:
                conf_matrix[curClassTrue][curClassPred] = 0
        
        # Update
        for curClassTrue, curClassPred in zip(real_list, predicted_list):
            conf_matrix[str(curClassTrue)][str(curClassPred)] = conf_matrix[str(curClassTrue)][str(curClassPred)] + 1

        # Output confusion matrix
        l.log("Confusion matrix:")
        sHeader = "\tPredicted\n"
        sHeader += "\t%s"%("\t".join(classes))
        l.log("%s"%(sHeader), no_date=True)

        sStr = ""
        for curClassTrue in classes:
            sStr += curClassTrue
            for curClassPred in classes:
                sStr += "\t" + str(conf_matrix[curClassTrue][curClassPred])
            sStr += "\n"
        l.log(sStr, no_date=True)
        
        sFooter = "^True"
        l.log("%s"%(sFooter), no_date=True)



    else:
        corr, p = stats.spearmanr(real_list, predicted_list)
        l.log("Correlation: %f (p-val: %f)"%(corr, p))

if __name__ == "__main__":
    main()