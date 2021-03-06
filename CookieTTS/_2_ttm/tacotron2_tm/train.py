import os
os.environ["LRU_CACHE_CAPACITY"] = "3"# reduces RAM usage massively with pytorch 1.4 or older
import time
import argparse
import math
import random
import pickle
import numpy as np
from numpy import finfo

import torch
from distributed import apply_gradient_allreduce
import torch.distributed as dist
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import ReduceLROnPlateau

from model import Tacotron2, ResGAN, DebluraGAN, load_model
from InfGAN import LSTMInferenceGAN, TemporalInferenceGAN
from CookieTTS.utils.dataset.data_utils import TTSDataset, Collate, generate_filelist_from_datasets
from CookieTTS.utils import get_args, force

from loss_function import Tacotron2Loss
from logger import Tacotron2Logger
from hparams import create_hparams
from CookieTTS.utils.model.GPU import to_gpu
import time
from math import e
from math import ceil

from tqdm import tqdm
import CookieTTS.utils.audio.stft as STFT
from CookieTTS.utils.dataset.utils import load_wav_to_torch, load_filepaths_and_text
from scipy.io.wavfile import read

import os.path

save_file_check_path = "save"
start_from_checkpoints_from_zero = 0

class LossExplosion(Exception):
    """Custom Exception Class. If Loss Explosion, raise Error and automatically restart training script from previous best_val_model checkpoint."""
    pass

def reduce_tensor(tensor, n_gpus):
    rt = tensor.clone()
    dist.all_reduce(rt, op=dist.ReduceOp.SUM)
    rt /= n_gpus
    return rt

def create_mels(hparams):
    stft = STFT.TacotronSTFT(
                hparams.filter_length, hparams.hop_length, hparams.win_length,
                hparams.n_mel_channels, hparams.sampling_rate, hparams.mel_fmin,
                hparams.mel_fmax)

    def save_mel(file):
        audio, sampling_rate = load_wav_to_torch(file)
        if sampling_rate != stft.sampling_rate:
            raise ValueError("{} {} SR doesn't match target {} SR".format(file,
                sampling_rate, stft.sampling_rate))
        melspec = stft.mel_spectrogram(audio.unsqueeze(0)).squeeze(0).cpu().numpy()
        np.save(file.replace('.wav', '.npy'), melspec)

    # Get the filepath for training and validation files
    wavs = [x[0] for x in load_filepaths_and_text(hparams.training_files) + load_filepaths_and_text(hparams.validation_files)]

    print(str(len(wavs))+" files being converted to mels")
    for audiopath in tqdm(wavs):
        try:
            save_mel(audiopath)
        except Exception as ex:
            tqdm.write(audiopath, " failed to process\n",ex,"\n")

    assert 0


class StreamingMovingAverage:
    def __init__(self, window_size):
        self.window_size = window_size
        self.values = []
        self.sum = 0

    def process(self, value):
        self.values.append(value)
        self.sum += value
        if len(self.values) > self.window_size:
            self.sum -= self.values.pop(0)
        return float(self.sum) / len(self.values)


def init_distributed(hparams, n_gpus, rank, group_name):
    assert torch.cuda.is_available(), "Distributed mode requires CUDA."
    print("Initializing Distributed")

    # Set cuda device so everything is done on the right GPU.
    torch.cuda.set_device(rank % torch.cuda.device_count())
    
    # Initialize distributed communication
    dist.init_process_group(
        backend=hparams.dist_backend, init_method=hparams.dist_url,
        world_size=n_gpus, rank=rank, group_name=group_name)

    print("Done initializing distributed")


def get_filelist(hparams, val=False):
    if hparams.data_source == 1:# if filelist is a folder, check all datasets inside the folder for audio files and transcripts.
        # convert dataset folder into a filelist
        filelist = generate_filelist_from_datasets(hparams.dataset_folder,
                                AUDIO_FILTER =hparams.dataset_audio_filters,
                                AUDIO_REJECTS=hparams.dataset_audio_rejects,
                                MIN_DURATION =hparams.dataset_min_duration,
                                MAX_DURATION =hparams.dataset_max_duration,
                                MIN_CHAR_LEN =hparams.dataset_min_chars,
                                MAX_CHAR_LEN =hparams.dataset_max_chars,)
    elif hparams.data_source == 0:# else filelist is a ".txt" file, load the easy way
        if val:
            filelist = load_filepaths_and_text(hparams.validation_files)
        else:
            filelist = load_filepaths_and_text(hparams.training_files)
    return filelist

def prepare_dataloaders(hparams, dataloader_args, args, speaker_ids, audio_offset=0):
    # Get data, data loaders and collate function ready
    if hparams.data_source == 1:
        if args.rank == 0:
            fl_dict = get_filelist(hparams)
        
        if args.n_gpus > 1:
            if args.rank == 0:
                with open('fl_dict.pkl', 'wb') as pickle_file:
                    pickle.dump(fl_dict, pickle_file, pickle.HIGHEST_PROTOCOL)
            torch.distributed.barrier()# wait till all graphics cards reach this point.
            if args.rank > 0:
                fl_dict = pickle.load(open('fl_dict.pkl', "rb"))
        
        speakerlist = fl_dict['speakerlist']
        
        filelist    = fl_dict['filelist']
        speaker_ids = fl_dict['speaker_ids']
        random.Random(0).shuffle(filelist)
        training_filelist   = filelist[ int(len(filelist)*hparams.dataset_p_val):]
        validation_filelist = filelist[:int(len(filelist)*hparams.dataset_p_val) ]

        
        if args.n_gpus > 1:
            torch.distributed.barrier()# wait till all graphics cards reach this point.
            if args.rank == 0 and os.path.exists('fl_dict.pkl'):
                os.remove('fl_dict.pkl')
    else:
        # get speaker names to ID
        if os.path.exists(hparams.speakerlist):
            # expects speakerlist to look like below
            # |Nancy|0
            # |Bob|1
            # |Phil|2
            #
            # every line must have at least 2 pipe symbols
            speakerlist = load_filepaths_and_text(hparams.speakerlist)
            speaker_name_lookup = {x[1]: speaker_id_lookup[x[2]] for x in speakerlist if x[2] in speaker_id_lookup.keys()}
        else:
            print("'speakerlist' in hparams.py not found! Speaker names will be IDs instead.")
            speakerlist = [['Dataset',i,i,'Source','Source Type'] for i in range(speaker_id_lookup.keys())]
            speaker_name_lookup = speaker_id_lookup# if there is no speaker
        
        training_filelist   = get_filelist(hparams, val=False)
        validation_filelist = get_filelist(hparams, val=True)
        speaker_ids = speaker_ids if hparams.use_saved_speakers else None
    
    trainset = TTSDataset(training_filelist, hparams, dataloader_args, check_files=hparams.check_files, shuffle=False,
                           deterministic_arpabet=False, speaker_ids=speaker_ids,          audio_offset=audio_offset)
    valset = TTSDataset(validation_filelist, hparams, dataloader_args, check_files=hparams.check_files, shuffle=False,
                           deterministic_arpabet=True,  speaker_ids=trainset.speaker_ids, audio_offset=audio_offset)
    collate_fn = Collate(hparams)
    
    #use_shuffle = False if hparams.use_TBPTT else True# can't shuffle with TBPTT
    use_shuffle = False# using custom Shuffle function inside dataloader.dataset which works with TBPTT
    if hparams.distributed_run:
        train_sampler = DistributedSampler(trainset, shuffle=use_shuffle)
        shuffle = False
    else:
        train_sampler = None
        shuffle = use_shuffle
    
    train_loader = DataLoader(trainset, shuffle=shuffle, sampler=train_sampler,
                              num_workers=hparams.num_workers,# prefetch_factor=hparams.prefetch_factor,
                              batch_size=hparams.batch_size, pin_memory=False,
                              drop_last=True, collate_fn=collate_fn)
    return train_loader, valset, collate_fn, train_sampler, trainset, speakerlist


def prepare_directories_and_logger(hparams, args):
    if args.rank == 0:
        if not os.path.isdir(args.output_directory):
            os.makedirs(args.output_directory)
            os.chmod(args.output_directory, 0o775)
        logger = Tacotron2Logger(os.path.join(args.output_directory, args.log_directory), hparams)
    else:
        logger = None
    return logger


def warm_start_force_model(checkpoint_path, model, resGAN, dbGAN, infGAN):
    assert os.path.isfile(checkpoint_path)
    print(f"Warm starting model from checkpoint '{checkpoint_path}'")
    
    if resGAN is not None and os.path.exists(checkpoint_path+'_resdis'):
        resGAN.load_state_dict_from_file(checkpoint_path+'_resdis')
    
    if dbGAN is not None and os.path.exists(checkpoint_path+'_dbDis'):
        dbGAN.load_state_dict_from_file(checkpoint_path+'_dbDis')
    
    if infGAN is not None and os.path.exists(checkpoint_path+'_InfGAN'):
        infGAN.load_state_dict_from_file(checkpoint_path+'_InfGAN')
    
    checkpoint_dict = torch.load(checkpoint_path, map_location='cpu')
    pretrained_dict = checkpoint_dict['state_dict']
    model_dict = model.state_dict()
    # Fiter out unneccessary keys
    filtered_dict = {k: v for k,v in pretrained_dict.items() if k in model_dict and pretrained_dict[k].shape == model_dict[k].shape}
    model_dict_missing = {k: v for k,v in pretrained_dict.items() if k not in model_dict}
    model_dict_mismatching = {k: v for k,v in pretrained_dict.items() if k in model_dict and pretrained_dict[k].shape != model_dict[k].shape}
    pretrained_missing = {k: v for k,v in model_dict.items() if k not in pretrained_dict}
    if model_dict_missing: print(list(model_dict_missing.keys()),'does not exist in the current model and is being ignored')
    if model_dict_mismatching: print(list(model_dict_mismatching.keys()),"is the wrong shape and has been reset")
    if pretrained_missing: print(list(pretrained_missing.keys()),"doesn't have pretrained weights and has been reset")
    model_dict.update(filtered_dict)
    model.load_state_dict(model_dict)
    
    iteration = 0
    if True:
        iteration = checkpoint_dict.get('iteration', 0)
    saved_lookup = checkpoint_dict['speaker_id_lookup'] if 'speaker_id_lookup' in checkpoint_dict.keys() else None
    return model, iteration, saved_lookup


def warm_start_model(checkpoint_path, model, resGAN, dbGAN, infGAN, ignore_layers):
    assert os.path.isfile(checkpoint_path)
    print("Warm starting model from checkpoint '{}'".format(checkpoint_path))
    
    if resGAN is not None and os.path.exists(checkpoint_path+'_resdis'):
        resGAN.load_state_dict_from_file(checkpoint_path+'_resdis')
    
    if dbGAN is not None and os.path.exists(checkpoint_path+'_dbDis'):
        dbGAN.load_state_dict_from_file(checkpoint_path+'_dbDis')
    
    if infGAN is not None and os.path.exists(checkpoint_path+'_InfGAN'):
        infGAN.load_state_dict_from_file(checkpoint_path+'_InfGAN')
    
    checkpoint_dict = torch.load(checkpoint_path, map_location='cpu')
    model_dict = checkpoint_dict['state_dict']
    if len(ignore_layers) > 0:
        model_dict = {k: v for k, v in model_dict.items()
                      if k not in ignore_layers}
        dummy_dict = model.state_dict()
        dummy_dict.update(model_dict)
        model_dict = dummy_dict
    model.load_state_dict(model_dict)
    iteration = checkpoint_dict['iteration']
    iteration = 0
    saved_lookup = checkpoint_dict['speaker_id_lookup'] if 'speaker_id_lookup' in checkpoint_dict.keys() else None
    return model, iteration, saved_lookup


def load_checkpoint(checkpoint_path, model, optimizer, resGAN, dbGAN, infGAN, best_val_loss_dict, best_loss_dict, best_validation_loss=1e3, best_inf_attsc=-99.):
    assert os.path.isfile(args.checkpoint_path)
    print("Loading checkpoint '{}'".format(args.checkpoint_path))
    
    if resGAN is not None and os.path.exists(checkpoint_path+'_resdis'):
        resGAN.load_state_dict_from_file(checkpoint_path+'_resdis')
    
    if dbGAN is not None and os.path.exists(checkpoint_path+'_dbDis'):
        dbGAN.load_state_dict_from_file(checkpoint_path+'_dbDis')
    
    if infGAN is not None and os.path.exists(checkpoint_path+'_InfGAN'):
        infGAN.load_state_dict_from_file(checkpoint_path+'_InfGAN')
    
    checkpoint_dict = torch.load(args.checkpoint_path, map_location='cpu')
    
    model.load_state_dict(checkpoint_dict['state_dict'])# load model weights
    
    if 'optimizer' in checkpoint_dict.keys():
        optimizer.load_state_dict(checkpoint_dict['optimizer'])# load optimizer state
    #if 'amp' in checkpoint_dict.keys() and amp is not None:
    #    amp.load_state_dict(checkpoint_dict['amp']) # load AMP (fp16) state.
    if 'learning_rate' in checkpoint_dict.keys():
        learning_rate = checkpoint_dict['learning_rate']
    #if 'hparams' in checkpoint_dict.keys():
    #    hparams = checkpoint_dict['hparams']
    if 'best_validation_loss' in checkpoint_dict.keys():
        best_validation_loss = checkpoint_dict['best_validation_loss']
    if 'best_inf_attsc' in checkpoint_dict.keys():
        best_inf_attsc = checkpoint_dict['best_inf_attsc']
    if 'best_val_loss_dict' in checkpoint_dict.keys():
        best_val_loss_dict = checkpoint_dict['best_val_loss_dict']
    if 'best_loss_dict' in checkpoint_dict.keys():
        best_loss_dict = checkpoint_dict['best_loss_dict']
    if 'average_loss' in checkpoint_dict.keys():
        average_loss = checkpoint_dict['average_loss']
	
    iteration = 0 if start_from_checkpoints_from_zero else checkpoint_dict['iteration']
    saved_lookup = checkpoint_dict['speaker_id_lookup'] if 'speaker_id_lookup' in checkpoint_dict.keys() else None
    
    print(f"Loaded checkpoint '{args.checkpoint_path}' from iteration {iteration}")
    return model, optimizer, learning_rate, iteration, best_validation_loss, best_inf_attsc, saved_lookup, best_val_loss_dict, best_loss_dict


def save_checkpoint(model, optimizer, resGAN, dbGAN, infGAN, learning_rate, iteration, hparams, best_validation_loss, best_inf_attsc, average_loss,
                    best_val_loss_dict, best_loss_dict, speaker_id_lookup, speakerlist, filepath):
    from CookieTTS.utils.dataset.utils import load_filepaths_and_text
    tqdm.write("Saving model and optimizer state at iteration {} to {}".format(
        iteration, filepath))
    
    if resGAN is not None:
        resGAN.save_state_dict(filepath+'_resdis')
    
    if dbGAN is not None:
        dbGAN.save_state_dict(filepath+'_dbDis')
    
    if infGAN is not None:
        infGAN.save_state_dict(filepath+'_InfGAN')
    
    assert all(str(line[2]).isdigit() for line in speakerlist), 'speakerlist got str in speaker_id section!'
    speaker_name_lookup = {x[1].strip(): x[2] for x in speakerlist}
    
    save_dict = {'iteration'           : iteration,
                 'state_dict'          : model.state_dict(),
                 'optimizer'           : optimizer.state_dict(),
                 'learning_rate'       : learning_rate,
                 'hparams'             : hparams,
                 'speaker_id_lookup'   : speaker_id_lookup,
                 'speaker_name_lookup' : speaker_name_lookup,
                 'speakerlist'         : speakerlist,
                 'best_validation_loss': best_validation_loss,
                 'best_inf_attsc'      : best_inf_attsc,
                 'best_val_loss_dict'  : best_val_loss_dict,
                 'best_loss_dict'      : best_loss_dict,
                 'average_loss'        : average_loss}
    if hparams.fp16_run:
        save_dict['amp'] = amp.state_dict()
    torch.save(save_dict, filepath)
    tqdm.write("Saving Complete")


def write_dict_to_file(file_losses, fpath, n_gpus, rank, deliminator='","', ends='"'):
    if n_gpus > 1:
        # synchronize data between graphics cards
        import pickle
        
        # dump file_losses for each graphics card into files
        with open(fpath+f'_rank{rank}', 'wb') as pickle_file:
            pickle.dump(file_losses, pickle_file, pickle.HIGHEST_PROTOCOL)
        
        torch.distributed.barrier()# wait till all graphics cards reach this point.
        
        # merge file losses from other graphics cards into this process
        list_of_dicts = []
        for f_rank in [x for x in list(range(n_gpus)) if x != rank]:
            list_of_dicts.append(pickle.load(open(f'{fpath}_rank{f_rank}', "rb")))
        
        for new_file_losses in list_of_dicts:
            for path, loss_dict in new_file_losses.items():
                if path in file_losses:
                    if loss_dict['time'] > file_losses[path]['time']:
                        file_losses[path] = loss_dict
                else:
                    file_losses[path] = loss_dict
        
        torch.distributed.barrier()# wait till all graphics cards reach this point.
        
        os.remove(fpath+f'_rank{rank}')
    
    if rank == 0:# write file_losses data to .CSV file.
        print(f"Writing CSV to {fpath}")
        with open(fpath, 'w') as f:
            f.write(ends+deliminator.join(['path',]+[str(key) for key in next(iter(file_losses.values())).keys()])+ends)
            for path, loss_dict in file_losses.items():
                line = []
                line.append(path)
                for loss_name, loss_value in loss_dict.items():
                    line.append(str(loss_value))
                f.write('\n'+ends+deliminator.join(line)+ends)
    
    return file_losses

def get_mse_sampled_filelist(original_filelist, file_losses, exp_factor, seed=None, max_weighting=8.0):
    # collect losses of each file for each speaker into lists
    speaker_losses = {}
    for loss_dict in file_losses.values():
        speaker_id = int(loss_dict['speaker_id_ext'])
        if speaker_id not in speaker_losses:
            speaker_losses[speaker_id] = {k:[v,] for k,v in list(loss_dict.items())[2:] if v is not None}
        else:
            for loss_name, loss_value in list(loss_dict.items())[2:]:
                if loss_name not in speaker_losses[speaker_id]:
                    speaker_losses[speaker_id][loss_name] = [loss_value,]
                elif loss_value is not None:
                    speaker_losses[speaker_id][loss_name].append(loss_value)
    assert len(speaker_losses.keys())
    
    # then average the loss list for each speaker
    speaker_avg_losses = {k: {} for k in sorted(speaker_losses.keys())}
    for speaker in speaker_avg_losses.keys():
        for loss_name in speaker_losses[speaker].keys():
            speaker_avg_losses[speaker][loss_name] = sum([x for x in speaker_losses[speaker][loss_name] if x is not None])/len(speaker_losses[speaker][loss_name])
    assert len(speaker_avg_losses.keys())
    
    # generate speaker filelists
    spkr_filelist = {int(spkr_id): [] for spkr_id in set([x[2] for x in original_filelist])}
    for path, quote, speaker_id, *_ in original_filelist:
        spkr_filelist[int(speaker_id)].append([path, quote, speaker_id, *_])
    assert len(spkr_filelist.keys()) and any(len(x) for x in spkr_filelist.values())
    
    # shuffle speaker filelists
    for k in spkr_filelist.keys():
        random.Random(seed).shuffle(spkr_filelist[k])
    
    # calculate dataset portion for each speaker and build new filelist
    dataset_len = len(original_filelist)
    new_filelist = []
    spec_MSE_total = sum([loss_dict['spec_MSE']**exp_factor for loss_dict in speaker_avg_losses.values()])
    for speaker_id, loss_dict in speaker_avg_losses.items():
        if int(speaker_id) in spkr_filelist.keys():
            sample_chance = (loss_dict['spec_MSE']**exp_factor)/spec_MSE_total# chance to sample this speaker
            assert sample_chance > 0.0
            n_files = round(sample_chance * dataset_len)
            spkr_files = spkr_filelist[int(speaker_id)]
            if (n_files == 0) or ( len(spkr_files) == 0 ):
                continue
            if len(spkr_files) < n_files:
                spkr_files = spkr_files * min(ceil(n_files/len(spkr_files)), max_weighting)# repeat filelist if needed
            new_filelist.extend(spkr_files[:n_files])
    assert len(new_filelist)
    
    return new_filelist

def update_smoothed_dict(orig_dict, new_dict, smoothing_factor=0.6):
    for key, value in new_dict.items():
        if key in orig_dict:# if audio file already in dict, merge new with old using smoothing_factor
            loss_names, loss_values = orig_dict[key].keys(), orig_dict[key].values()
            for loss_name in loss_names:
                if all(loss_name in dict_ for dict_ in [orig_dict[key], new_dict[key]]) and all(type(loss) in [int, float] for loss in [orig_dict[key][loss_name], new_dict[key][loss_name]]):
                    orig_dict[key][loss_name] = orig_dict[key][loss_name]*(smoothing_factor) + new_dict[key][loss_name]*(1-smoothing_factor)
                elif loss_name in new_dict[key] and type(new_dict[key][loss_name]) in [int, float]:
                    orig_dict[key][loss_name] = new_dict[key][loss_name]
        
        else:# if audio file not in dict, assign new key to dict
            orig_dict[key] = new_dict[key]
    return orig_dict
    

def validate(hparams, args, file_losses, model, criterion, valset, best_val_loss_dict, iteration,
             collate_fn, logger, val_teacher_force_till, val_p_teacher_forcing, teacher_force=-1):
    """Handles all the validation scoring and printing"""
    assert teacher_force >= 0, 'teacher_force not specified.'
    model.eval()
    orig_himode = model.decoder.half_inference_mode
    model.decoder.half_inference_mode = False
    with torch.no_grad():
        if hparams.inference_equally_sample_speakers and teacher_force == 2:# if inference, sample from each speaker equally. So speakers with smaller datasets get the same weighting onto the val loss.
            orig_filelist = valset.filelist
            valset.update_filelist(get_mse_sampled_filelist(orig_filelist, file_losses, 0.0, seed=1234))
        if not len(valset.filelist) >= hparams.n_gpus*hparams.batch_size:
            print(f'too few files in validation set! Found {len(valset.filelist)}, expected {hparams.batch_size} or more. If your dataset has single speaker, you can change "inference_equally_sample_speakers" to False in hparams.py which *may* fix the issue.\nIf you have a small amount of data, increase `dataset_p_val` or decrease `val_batch_size`')
        val_sampler = DistributedSampler(valset) if hparams.distributed_run else None
        val_loader = DataLoader(valset, sampler=val_sampler,
                                num_workers=hparams.val_num_workers,# prefetch_factor=hparams.prefetch_factor,
                                shuffle=False, batch_size=hparams.batch_size,
                                pin_memory=False, drop_last=False, collate_fn=collate_fn)
        
        loss_dict_total = None
        for i, batch in tqdm(enumerate(val_loader), desc="Validation", total=len(val_loader), smoothing=0): # i = index, batch = stuff in array[i]
            y = model.parse_batch(batch)
            with torch.random.fork_rng(devices=[0,]):
                torch.random.manual_seed(i)# use repeatable seeds during validation so results are more consistent and comparable.
                y_pred = force(model, valid_kwargs=model_args, **{**y, "teacher_force_till": val_teacher_force_till, "p_teacher_forcing": val_p_teacher_forcing})
            
            val_loss_scalars = {
                 "spec_MSE_weight": 0.00,
                "spec_MFSE_weight": 1.00,
              "postnet_MSE_weight": 0.00,
             "postnet_MFSE_weight": 1.00,
                "gate_loss_weight": 1.00,
                "sylps_kld_weight": 0.00,
                "sylps_MSE_weight": 0.00,
                "sylps_MAE_weight": 0.05,
             "res_enc_gMSE_weight": 0.00,
             "res_enc_dMSE_weight": 0.00,
              "res_enc_kld_weight": 0.00,
                 "diag_att_weight": 0.00,
              "dbGAN_gLoss_weight": 0.00,
              "dbGAN_dLoss_weight": 0.00,
             "InfGAN_gLoss_weight": 0.00,
             "InfGAN_dLoss_weight": 0.00,
            }
            loss_dict, file_losses_batch = criterion(model, y_pred, y, val_loss_scalars)
            file_losses = update_smoothed_dict(file_losses, file_losses_batch, file_losses_smoothness)
            if loss_dict_total is None:
                loss_dict_total = {k: 0. for k, v in loss_dict.items()}
            
            if hparams.distributed_run:
                reduced_loss_dict = {k: reduce_tensor(v.data, args.n_gpus).item() if v is not None else 0. for k, v in loss_dict.items()}
            else:
                reduced_loss_dict = {k: v.item() if v is not None else 0. for k, v in loss_dict.items()}
            reduced_loss = reduced_loss_dict['loss']
            
            for k in loss_dict_total.keys():
                loss_dict_total[k] = loss_dict_total[k] + reduced_loss_dict[k]
            # end forloop
        loss_dict_total = {k: v/(i+1) for k, v in loss_dict_total.items()}
        # end torch.no_grad()
    
    # reverse changes to valset and model
    if hparams.inference_equally_sample_speakers and teacher_force == 2:# if inference, sample from each speaker equally. So speakers with smaller datasets get the same weighting onto the val loss.
        valset.update_filelist(orig_filelist)
    model.train()
    model.decoder.half_inference_mode = orig_himode
    
    # update best losses
    if best_val_loss_dict is None:
        best_val_loss_dict = loss_dict_total
    else:
        best_val_loss_dict = {k: min(best_val_loss_dict[k], loss_dict_total[k]) for k in best_val_loss_dict.keys()}
    
    # print, log data and return.
    if args.rank == 0:
        tqdm.write(f"Validation loss {iteration}: {loss_dict_total['loss']:9f}  Average Max Attention: {loss_dict_total['avg_max_attention']:9f}")
        if iteration > 1:
            log_terms = (loss_dict_total, best_val_loss_dict, model, y, y_pred, iteration, val_teacher_force_till, val_p_teacher_forcing)
            if teacher_force == 2:
                logger.log_infer(*log_terms)
            else:
                logger.log_validation(*log_terms)
    
    if teacher_force == 2:
        return loss_dict_total['weighted_score'], best_val_loss_dict, file_losses
    else:
        return loss_dict_total['loss'], best_val_loss_dict, file_losses
    


def calculate_global_mean(data_loader, global_mean_npy, hparams):
    if global_mean_npy and os.path.exists(global_mean_npy):
        global_mean = np.load(global_mean_npy)
        return to_gpu(torch.tensor(global_mean).half()) if hparams.fp16_run else to_gpu(torch.tensor(global_mean).float())
    sums = []
    frames = []
    print('calculating global mean...')
    for i, batch in tqdm(enumerate(data_loader), total=len(data_loader), smoothing=0.001):
        # padded values are 0.
        sums.append(batch['gt_mel'].double().sum(dim=(0, 2)))
        frames.append(batch['mel_lengths'].double().sum())
        if i > 100:
            break
    global_mean = sum(sums) / sum(frames)
    global_mean = to_gpu(global_mean.half()) if hparams.fp16_run else to_gpu(global_mean.float())
    if global_mean_npy:
        np.save(global_mean_npy, global_mean.cpu().numpy())
    return global_mean


def train(args, rank, group_name, hparams):
    """Training and validation logging results to tensorboard and stdout

    Params
    ------
    args.output_directory (string): directory to save checkpoints
    args.log_directory (string) directory to save tensorboard logs
    args.checkpoint_path(string): checkpoint path
    args.n_gpus (int): number of gpus
    rank (int): rank of current gpu
    hparams (object): comma separated list of "name=value" pairs.
    """
    # setup distributed
    hparams.n_gpus = args.n_gpus
    hparams.rank = rank
    if hparams.distributed_run:
        init_distributed(hparams, args.n_gpus, rank, group_name)
    
    # reproducablilty stuffs
    torch.manual_seed(hparams.seed)
    torch.cuda.manual_seed(hparams.seed)
    
    # initialize blank model
    print('Initializing Tacotron2...')
    model = load_model(hparams)
    print('Done')
    global model_args
    model_args = get_args(model.forward)
    model.eval()
    learning_rate = hparams.learning_rate
    
    # (optional) show the names of each layer in model, mainly makes it easier to copy/paste what you want to adjust
    if hparams.print_layer_names_during_startup:
        print(*[f"Layer{i} = "+str(x[0])+" "+str(x[1].shape) for i,x in enumerate(list(model.named_parameters()))], sep="\n")
    
    # (optional) Freeze layers by disabling grads
    if len(hparams.frozen_modules):
        for layer, params in list(model.named_parameters()):
            if any(layer.startswith(module) for module in hparams.frozen_modules):
                params.requires_grad = False
                print(f"Layer: {layer} has been frozen")
    
    if len(hparams.unfrozen_modules):
        for layer, params in list(model.named_parameters()):
            if any(layer.startswith(module) for module in hparams.unfrozen_modules):
                params.requires_grad = True
                print(f"Layer: {layer} has been unfrozen")
    
    # define optimizer (any params without requires_grad are ignored)
    optimizer =  torch.optim.Adam(filter(lambda p: p.requires_grad, model.parameters()), lr=learning_rate, weight_decay=hparams.weight_decay)
    #optimizer = apexopt.FusedAdam(filter(lambda p: p.requires_grad, model.parameters()), lr=learning_rate, weight_decay=hparams.weight_decay)
    
    resGAN = None
    if hparams.use_res_enc:
        resGAN = ResGAN(hparams).cuda()
        resGAN.optimizer =  torch.optim.Adam(filter(lambda p: p.requires_grad, resGAN.discriminator.parameters()), lr=learning_rate, weight_decay=hparams.weight_decay)
        #resGAN.optimizer = apexopt.FusedAdam(filter(lambda p: p.requires_grad, resGAN.discriminator.parameters()), lr=learning_rate, weight_decay=hparams.weight_decay)
        if hparams.distributed_run:
            resGAN.discriminator = apply_gradient_allreduce(resGAN.discriminator)
    
    dbGAN = None
    if hparams.use_dbGAN:
        dbGAN = DebluraGAN(hparams).cuda()
        dbGAN.optimizer =  torch.optim.Adam(filter(lambda p: p.requires_grad, dbGAN.discriminator.parameters()), lr=learning_rate, weight_decay=hparams.weight_decay)
        #dbGAN.optimizer = apexopt.FusedAdam(filter(lambda p: p.requires_grad, dbGAN.discriminator.parameters()), lr=learning_rate, weight_decay=hparams.weight_decay)
        if hparams.distributed_run:
            dbGAN.discriminator = apply_gradient_allreduce(dbGAN.discriminator)
    
    infGAN = None
    if hparams.use_InfGAN:
        infGAN = TemporalInferenceGAN(hparams).cuda() if hparams.TemporalInfGAN else LSTMInferenceGAN(hparams).cuda()
        infGAN.optimizer =  torch.optim.Adam(filter(lambda p: p.requires_grad, infGAN.discriminator.parameters()), lr=learning_rate, weight_decay=hparams.weight_decay)
        #infGAN.optimizer = apexopt.FusedAdam(filter(lambda p: p.requires_grad, infGAN.discriminator.parameters()), lr=learning_rate, weight_decay=hparams.weight_decay)
        if hparams.distributed_run:
            infGAN.discriminator = apply_gradient_allreduce(infGAN.discriminator)
    
    if True and rank == 0:
        pytorch_total_params = sum(p.numel() for p in model.parameters())
        print("{:,} total parameters in model".format(pytorch_total_params))
        pytorch_total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
        print("{:,} trainable parameters.".format(pytorch_total_params))
    
    print("Initializing AMP Model(s) / Optimzier(s)")
    if hparams.fp16_run:
        models     = [model,]
        optimizers = [optimizer,]
        
        if hparams.use_res_enc:
            models.append(resGAN.discriminator)
            optimizers.append(resGAN.optimizer)
        if hparams.use_dbGAN:
            models.append(dbGAN.discriminator)
            optimizers.append(dbGAN.optimizer)
        if hparams.use_InfGAN:
            models.append(infGAN.discriminator)
            optimizers.append(infGAN.optimizer)
        models, optimizers = amp.initialize(models, optimizers, opt_level=f'O{hparams.fp16_run_optlvl}')
        if hparams.use_InfGAN:
            infGAN.discriminator = models.pop()
            infGAN.optimizer     = optimizers.pop()
            infGAN.amp = amp
        if hparams.use_dbGAN:
            dbGAN.discriminator = models.pop()
            dbGAN.optimizer     = optimizers.pop()
            dbGAN.amp  = amp
        if hparams.use_res_enc:
            resGAN.discriminator = models.pop()
            resGAN.optimizer     = optimizers.pop()
            resGAN.amp = amp
        model     = models.pop()
        optimizer = optimizers.pop()
        assert     len(models) == 0
        assert len(optimizers) == 0
        del models, optimizers
    
    print("Initializing Gradient AllReduce model wrapper.")
    if hparams.distributed_run:
        model = apply_gradient_allreduce(model)
    
    print("Initializing Tacotron2 Loss func.")
    criterion = Tacotron2Loss(hparams)
    
    print("Initializing Tacotron2 Logger.")
    logger = prepare_directories_and_logger(hparams, args)
    
    # Load checkpoint if one exists
    best_validation_loss = 1e3# used to see when "best_val_model" should be saved
    best_inf_attsc       = -99# used to see when "best_inf_attsc" should be saved
    
    is_overflow = False
    average_loss = 9e9
    n_restarts = 0
    checkpoint_iter = 0
    iteration = 0
    epoch_offset = 0
    _learning_rate = 1e-3
    saved_lookup = None
    original_filelist = None
    
    global file_losses
    file_losses = {}
    global file_losses_smoothness
    file_losses_smoothness = 0.6
    
    global best_val_loss_dict
    best_val_loss_dict = None
    global best_loss_dict
    best_loss_dict = None
    global expavg_loss_dict
    expavg_loss_dict = None
    expavg_loss_dict_iters = 0# initial iters expavg_loss_dict has been fitted
    loss_dict_smoothness = 0.95 # smoothing factor
    
    if args.checkpoint_path is not None:
        if args.warm_start:
            model, iteration, saved_lookup = warm_start_model(
                args.checkpoint_path, model, resGAN, dbGAN, infGAN, hparams.ignore_layers)
        elif args.warm_start_force:
            model, iteration, saved_lookup = warm_start_force_model(
                args.checkpoint_path, model, resGAN, dbGAN, infGAN)
        else:
            _ = load_checkpoint(args.checkpoint_path, model, optimizer, resGAN, dbGAN, infGAN, best_val_loss_dict, best_loss_dict)
            model, optimizer, _learning_rate, iteration, best_validation_loss, best_inf_attsc, saved_lookup, best_val_loss_dict, best_loss_dict = _
            if hparams.use_saved_learning_rate:
                learning_rate = _learning_rate
        checkpoint_iter = iteration
        iteration += 1  # next iteration is iteration + 1
        print('Model Loaded')
    
    # define datasets/dataloaders
    dataloader_args = [*get_args(criterion.forward), *model_args]
    if rank == 0:
        dataloader_args.extend(get_args(logger.log_training))
    train_loader, valset, collate_fn, train_sampler, trainset, speakerlist = prepare_dataloaders(hparams, dataloader_args, args, saved_lookup)
    epoch_offset = max(0, int(iteration / len(train_loader)))
    speaker_lookup = trainset.speaker_ids
    
    # load and/or generate global_mean
    if hparams.drop_frame_rate > 0.:
        if rank != 0: # if global_mean not yet calcuated, wait for main thread to do it
            while not os.path.exists(hparams.global_mean_npy): time.sleep(1)
        global_mean = calculate_global_mean(train_loader, hparams.global_mean_npy, hparams)
        assert hparams.n_mel_channels == global_mean.squeeze().shape[0], f'{hparams.global_mean_npy} has {global_mean.squeeze().shape[0]} channels, expected {hparams.n_mel_channels}.\nDelete the file if unsure.'
        hparams.global_mean = global_mean
        model.global_mean = global_mean
    
    # define scheduler
    use_scheduler = 0
    if use_scheduler:
        scheduler = ReduceLROnPlateau(optimizer, factor=0.1**(1/5), patience=10)
    
    model.train()
    
    for param_group in optimizer.param_groups:
        param_group['lr'] = learning_rate
    
    just_did_val = True
    rolling_loss = StreamingMovingAverage(min(int(len(train_loader)), 200))
    
    # reproducablilty stuffs
    torch.manual_seed(hparams.seed)
    torch.cuda.manual_seed(hparams.seed)

    # Run Simulated Largest Input to Cache VRAM Chunks
    print("Running Simulated Largest Possible Input to Pre-Allocate VRAM")
    with torch.random.fork_rng(devices=[0,]):
        batch = next(iter(train_loader))
        batch['text']            = batch['text'].new_zeros(batch['text'].shape[0], hparams.max_chars_length).long()
        batch['text_lengths'][0] = hparams.max_chars_length
        batch['gt_mel']          = batch['gt_mel'].new_zeros(batch['text'].shape[0], hparams.n_mel_channels, hparams.max_segment_length)
        batch['mel_lengths' ][0] = hparams.max_segment_length
        batch['gt_gate_logits' ] = batch['gt_gate_logits'].new_zeros(batch['text'].shape[0], hparams.max_segment_length)
        optimizer.zero_grad()
        y = model.parse_batch(batch) # move batch to GPU (async)
        y_pred = force(model, valid_kwargs=model_args, **{**y, "teacher_force_till": 0, "p_teacher_forcing": 0.0, "drop_frame_rate": 0.1})        
        loss_scalars = {
             "spec_MSE_weight": 0.0,
            "spec_MFSE_weight": 1.0,
          "postnet_MSE_weight": 0.0,
         "postnet_MFSE_weight": 0.0,
            "gate_loss_weight": 0.0,
            "sylps_kld_weight": 0.0,
            "sylps_MSE_weight": 0.0,
            "sylps_MAE_weight": 0.0,
         "res_enc_gMSE_weight": 0.0,
         "res_enc_dMSE_weight": 0.0,
          "res_enc_kld_weight": 0.0,
             "diag_att_weight": 0.0,
          "dbGAN_gLoss_weight": 0.0,
          "dbGAN_dLoss_weight": 0.0,
         "InfGAN_gLoss_weight": 0.0,
         "InfGAN_dLoss_weight": 0.0,
          "teacher_force_till": 0.0,
           "p_teacher_forcing": 1.0,
        }
        loss_dict, file_losses_batch = criterion(model, y_pred, y, loss_scalars, resGAN if hparams.use_res_enc else None, dbGAN if hparams.use_dbGAN else None,)
        loss = loss_dict['loss']
        
        if hparams.distributed_run:
            reduced_loss_dict = {k: reduce_tensor(v.data, args.n_gpus).item() if v is not None else 0. for k, v in loss_dict.items()}
        else:
            reduced_loss_dict = {k: v.item() if v is not None else 0. for k, v in loss_dict.items()}
        
        reduced_loss = reduced_loss_dict['loss']
        
        if hparams.fp16_run:
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()
        
        if hparams.fp16_run:
            grad_norm = torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), 1e-9)
            is_overflow = math.isinf(grad_norm) or math.isnan(grad_norm)
        else:
            grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1e-9)
        
        for param_group in optimizer.param_groups:
            param_group['lr'] = 0.0
        #optimizer.step()
        del grad_norm, reduced_loss, loss_dict, file_losses_batch, y, y_pred, batch, loss_scalars
    print("Finished Pre-Allocating VRAM.")
    
    # reproducablilty stuffs
    torch.manual_seed(hparams.seed)
    torch.cuda.manual_seed(hparams.seed)
    
    # ================ MAIN TRAINNIG LOOP! ===================
    training = True
    while training:
        try:
            for epoch in tqdm(range(epoch_offset, hparams.epochs), initial=epoch_offset, total=hparams.epochs, desc="Epoch:", position=1, unit="epoch"):
                tqdm.write("Epoch:{}".format(epoch))
                
                train_loader.dataset.shuffle_dataset()# Shuffle Dataset
                dataset_len = len(train_loader)
                
                start_time = time.time()
                # start iterating through the epoch
                for i, batch in tqdm(enumerate(train_loader), desc="Iter:  ", smoothing=0, total=len(train_loader), position=0, unit="iter"):
                    # run external code every epoch or 1000 iters, allows the run to be adjusted without restarts
                    if (i==0 or iteration % param_interval == 0):
                        try:
                            with open("run_every_epoch.py", encoding='utf-8') as f:
                                internal_text = str(f.read())
                                if len(internal_text) > 0:
                                    #code = compile(internal_text, "run_every_epoch.py", 'exec')
                                    ldict = {'iteration': iteration, 'checkpoint_iter': checkpoint_iter, 'n_restarts': n_restarts}
                                    exec(internal_text, globals(), ldict)
                                else:
                                    print("[info] tried to execute 'run_every_epoch.py' but it is empty")
                        except Exception as ex:
                            print(f"[warning] 'run_every_epoch.py' FAILED to execute!\nException:\n{ex}")
                        globals().update(ldict)
                        locals().update(ldict)
                        if show_live_params:
                            print(internal_text)
                    n_restarts = n_restarts_override if (n_restarts_override is not None) else n_restarts or 0
                    # Learning Rate Schedule
                    if custom_lr:
                        if iteration < warmup_start:
                            learning_rate = warmup_start_lr
                        elif iteration < warmup_end:
                            learning_rate = (iteration-warmup_start)*((A_+C_)-warmup_start_lr)/(warmup_end-warmup_start) + warmup_start_lr # learning rate increases from warmup_start_lr to A_ linearly over (warmup_end-warmup_start) iterations.
                        else:
                            if iteration < decay_start:
                                learning_rate = A_ + C_
                            else:
                                iteration_adjusted = iteration - decay_start
                                learning_rate = (A_*(e**(-iteration_adjusted/B_))) + C_
                        assert learning_rate > -1e-8, "Negative Learning Rate."
                        if decrease_lr_on_restart:
                            learning_rate = learning_rate/(2**(n_restarts/3))
                        if just_did_val:
                            learning_rate = 0.0
                            just_did_val=False
                        for param_group in optimizer.param_groups:
                            param_group['lr'] = learning_rate
                    
                    if hparams.use_InfGAN:
                        model.decoder.return_attention_contexts    = infGAN.use_context
                        model.decoder.return_decoder_rnn_outputs   = infGAN.use_DecRNN
                        model.decoder.return_attention_rnn_outputs = infGAN.use_AttRNN
                        if model.decoder.half_inference_mode < enable_InfGAN:
                            print("Half Inference Training Mode Enabled! Batch Size is now 50% Reconstruction Loss, 50% InferenceGAN Loss!")
                        elif model.decoder.half_inference_mode > enable_InfGAN:
                            print("Half Inference Training Mode Disabled!")
                        model.decoder.half_inference_mode = bool(enable_InfGAN)
                    # /run external code every epoch, allows the run to be adjusting without restarts/
                    optimizer.zero_grad()
                    y = model.parse_batch(batch) # move batch to GPU (async)
                    y['gt_mel'].requires_grad_(True)
                    y_pred = force(model, valid_kwargs=model_args, **{**y, "teacher_force_till": teacher_force_till, "p_teacher_forcing": p_teacher_forcing, "drop_frame_rate": drop_frame_rate})
                    
                    loss_scalars = {
                         "spec_MSE_weight": spec_MSE_weight,
                        "spec_MFSE_weight": spec_MFSE_weight,
                      "postnet_MSE_weight": postnet_MSE_weight,
                     "postnet_MFSE_weight": postnet_MFSE_weight,
                        "gate_loss_weight": gate_loss_weight,
                        "sylps_kld_weight": sylps_kld_weight,
                        "sylps_MSE_weight": sylps_MSE_weight,
                        "sylps_MAE_weight": sylps_MAE_weight,
                     "res_enc_gMSE_weight": res_enc_gMSE_weight,
                     "res_enc_dMSE_weight": res_enc_dMSE_weight,
                      "res_enc_kld_weight": res_enc_kld_weight,
                         "diag_att_weight": diag_att_weight,
                      "dbGAN_gLoss_weight": dbGAN_gLoss_weight,
                      "dbGAN_dLoss_weight": dbGAN_dLoss_weight,
                     "InfGAN_gLoss_weight": InfGAN_gLoss_weight,
                     "InfGAN_dLoss_weight": InfGAN_dLoss_weight,
                      "teacher_force_till": teacher_force_till,
                       "p_teacher_forcing": p_teacher_forcing,
                    }
                    loss_dict, file_losses_batch = criterion(model, y_pred, y, loss_scalars,
                                                            resGAN if hparams.use_res_enc else None,
                                                            dbGAN  if hparams.use_dbGAN   else None,
                                                            infGAN if hparams.use_InfGAN  else None,)
                    
                    file_losses = update_smoothed_dict(file_losses, file_losses_batch, file_losses_smoothness)
                    
                    if hparams.distributed_run:
                        reduced_loss_dict = {k: reduce_tensor(v.data, args.n_gpus).item() if v is not None else 0. for k, v in loss_dict.items()}
                    else:
                        reduced_loss_dict = {k: v.item() if v is not None else 0. for k, v in loss_dict.items()}
                    
                    reduced_loss = reduced_loss_dict['loss']
                    
                    loss = loss_dict['loss']
                    if hparams.fp16_run:
                        with amp.scale_loss(loss, optimizer) as scaled_loss:
                            scaled_loss.backward()
                    else:
                        loss.backward()
                    
                    if rank==0 and show_gradients:
                        try:
                            _=avg_grads
                        except:
                            avg_grads = {}
                        for param_name, params in model.named_parameters():
                            if params.requires_grad and params.grad is not None:
                                norm_grad = 1.0
                                grad = params.grad.abs().sum().item()
                                if param_name not in avg_grads:
                                    avg_grads[param_name] = grad
                                elif grad*5. < avg_grads[param_name]:
                                    avg_grads[param_name] = (avg_grads[param_name]*0.9)+(grad*0.1)
                                norm_grad = grad/avg_grads[param_name]
                                if grad > 30.0 or norm_grad > 2.0:
                                    print(f'{norm_grad:03.1f} | {grad:020.6f} | {params.grad.abs().mean().item():06.6f}| {params.grad.abs().max().item():010.6f} | {param_name}')
                    
                    #with torch.no_grad(): # print collected gradients at the gt_mel
                    #    print(f"{y['gt_mel'].grad.chunk(2, dim=0)[0].abs().sum().item():06.2f}InputGrad0, {y['gt_mel'].grad.chunk(2, dim=0)[1].abs().sum().item():06.2f}InputGrad1")
                    
                    if grad_clip_thresh:
                        if hparams.fp16_run:
                            grad_norm = torch.nn.utils.clip_grad_norm_(
                                amp.master_params(optimizer), grad_clip_thresh)
                            is_overflow = math.isinf(grad_norm) or math.isnan(grad_norm)
                        else:
                            grad_norm = torch.nn.utils.clip_grad_norm_(
                                model.parameters(), grad_clip_thresh)
                    else:
                        grad_norm = 0.0
                    
                    optimizer.step()
                    
                    # calcuate the effective learning rate after gradient clipping is applied, and use the effective learning rate on the GAN modules.
                    effective_lr = 0.0 if is_overflow else (learning_rate*min((grad_clip_thresh/grad_norm+1e-6), 1.0) if grad_clip_thresh else learning_rate)
                    
                    # (Optional) Discriminator Forward+Backward Pass
                    if hparams.use_res_enc:
                        with torch.random.fork_rng(devices=[0,]):
                            for param_group in resGAN.optimizer.param_groups:
                                param_group['lr'] = effective_lr
                            resGAN(y_pred, reduced_loss_dict, loss_dict, loss_scalars)
                    
                    if 1 and hparams.use_dbGAN:
                        with torch.random.fork_rng(devices=[0,]):
                            for param_group in dbGAN.optimizer.param_groups:
                                param_group['lr'] = effective_lr
                            dbGAN(y_pred, y, reduced_loss_dict, loss_dict, loss_scalars)
                    
                    if hparams.use_InfGAN and model.decoder.half_inference_mode:
                        with torch.random.fork_rng(devices=[0,]):
                            expavgInfGANAcc = expavg_loss_dict.get('InfGAN_accuracy', None) if expavg_loss_dict is not None else None
                            InfGANAcc = expavgInfGANAcc or reduced_loss_dict.get('InfGAN_accuracy', 0.0)
                            for param_group in infGAN.optimizer.param_groups:
                                param_group['lr'] = effective_lr if (InfGANAcc < InfGAN_max_accuracy) else 0.0
                            infGAN(model, y_pred, y, reduced_loss_dict, loss_dict, loss_scalars)
                    
                    # get current Loss Scale of first optimizer
                    loss_scale = amp._amp_state.loss_scalers[0]._loss_scale if hparams.fp16_run else 32768.
                    
                    # restart if training/model has collapsed
                    if (iteration > 1e3 and (reduced_loss > LossExplosionThreshold)) or (math.isnan(reduced_loss)):
                        raise LossExplosion(f"\nLOSS EXPLOSION EXCEPTION ON RANK {rank}: Loss reached {reduced_loss} during iteration {iteration}.\n\n\n")
                    if (loss_scale < 1/4):
                        raise LossExplosion(f"\nLOSS EXCEPTION ON RANK {rank}: Loss Scaler reached {loss_scale} during iteration {iteration}.\n\n\n")
                    
                    if expavg_loss_dict is None:
                        expavg_loss_dict = reduced_loss_dict
                    else:
                        expavg_loss_dict.update({k:v for k, v in reduced_loss_dict.items() if k not in expavg_loss_dict.keys()})# if new loss term appears in reduced_loss_dict, add it to the expavg_loss_dict.
                        expavg_loss_dict = {k: (reduced_loss_dict[k]*(1-loss_dict_smoothness))+(expavg_loss_dict[k]*loss_dict_smoothness) for k in expavg_loss_dict.keys() if k in reduced_loss_dict}
                        expavg_loss_dict_iters += 1
                    
                    if expavg_loss_dict_iters > 100:
                        if best_loss_dict is None:
                            best_loss_dict = expavg_loss_dict
                        else:
                            best_loss_dict = {k: min(best_loss_dict[k], expavg_loss_dict[k]) for k in best_loss_dict.keys() if k in expavg_loss_dict}
                    
                    if rank == 0:
                        duration = time.time() - start_time
                        if not is_overflow:
                            average_loss = rolling_loss.process(reduced_loss)
                            InfGANAccStr = expavg_loss_dict.get('InfGAN_accuracy', None) or reduced_loss_dict.get('InfGAN_accuracy', 0.0)
                            WScoreStr    = expavg_loss_dict.get('weighted_score' , None) or reduced_loss_dict.get('weighted_score' , 0.0)
                            tqdm.write(
                                f"{iteration} [Train_loss:{reduced_loss:.3f} Avg:{average_loss:.3f}] "
                                f"[Grad Norm {grad_norm:04.2f}] [{duration:.2f}s/it] "
                                f"[{(duration/(hparams.batch_size*args.n_gpus)):.3f}s/file] "
                                f"[{learning_rate:.1e}LR] [{loss_scale:.0f}LS] "
                                f"[{WScoreStr:.1%}AttSc] [{InfGANAccStr:.1%}InfGANAcc]")
                            logger.log_training(model, reduced_loss_dict, expavg_loss_dict, best_loss_dict, grad_norm, learning_rate, duration, iteration, teacher_force_till, p_teacher_forcing, drop_frame_rate)
                        else:
                            tqdm.write("Gradient Overflow, Skipping Step")
                        start_time = time.time()
                    
                    if iteration%checkpoint_interval==0 or os.path.exists(save_file_check_path):
                        # save model checkpoint like normal
                        if rank == 0:
                            checkpoint_path = os.path.join(args.output_directory, "checkpoint_{}".format(iteration))
                            save_checkpoint(model, optimizer, resGAN, dbGAN, infGAN, learning_rate, iteration, hparams, best_validation_loss, best_inf_attsc, average_loss, best_val_loss_dict, best_loss_dict, speaker_lookup, speakerlist, checkpoint_path)
                    
                    if iteration%dump_filelosses_interval==0:
                        print("Updating File_losses dict!")
                        file_losses = write_dict_to_file(file_losses, os.path.join(args.output_directory, 'file_losses.csv'), args.n_gpus, rank)
                    
                    if (iteration % int(validation_interval) == 0) or (os.path.exists(save_file_check_path)) or (iteration < 1000 and (iteration % 250 == 0)):
                        if rank == 0 and os.path.exists(save_file_check_path):
                            os.remove(save_file_check_path)
                        # perform validation and save "best_val_model" depending on validation loss
                        val_loss, best_val_loss_dict, file_losses = validate(hparams, args, file_losses, model, criterion, valset, best_val_loss_dict, iteration, collate_fn, logger, val_teacher_force_till, val_p_teacher_forcing, teacher_force=0)# validate/teacher_force
                        file_losses = write_dict_to_file(file_losses, os.path.join(args.output_directory, 'file_losses.csv'), args.n_gpus, rank)
                        valatt_loss, *_ = validate(hparams, args, file_losses, model, criterion, valset, best_val_loss_dict, iteration, collate_fn, logger, 0, 0.0, teacher_force=2)# infer
                        if use_scheduler:
                            scheduler.step(val_loss)
                        if (val_loss < best_validation_loss):
                            best_validation_loss = val_loss
                            if rank == 0 and hparams.save_best_val_model:
                                checkpoint_path = os.path.join(args.output_directory, "best_val_model")
                                save_checkpoint(
                                    model, optimizer, resGAN, dbGAN, infGAN, learning_rate, iteration, hparams, best_validation_loss, max(best_inf_attsc, val_loss),
                                    average_loss, best_val_loss_dict, best_loss_dict, speaker_lookup, speakerlist, checkpoint_path)
                        if (valatt_loss > best_inf_attsc):
                            best_inf_attsc = valatt_loss
                            if rank == 0 and hparams.save_best_inf_attsc:
                                checkpoint_path = os.path.join(args.output_directory, "best_inf_attsc")
                                save_checkpoint(
                                    model, optimizer, resGAN, dbGAN, infGAN, learning_rate, iteration, hparams, best_validation_loss, best_inf_attsc,
                                    average_loss, best_val_loss_dict, best_loss_dict, speaker_lookup, speakerlist, checkpoint_path)
                        just_did_val = True
                    
                    del y_pred, y, batch, loss_dict, reduced_loss_dict
                    iteration += 1
                    # end of iteration loop
                
                # update filelist of training dataloader
                if (iteration > hparams.min_avg_max_att_start) and (iteration-checkpoint_iter >= dataset_len):
                    print("Updating File_losses dict!")
                    file_losses = write_dict_to_file(file_losses, os.path.join(args.output_directory, 'file_losses.csv'), args.n_gpus, rank)
                    print("Done!")
                    
                    print("Updating dataloader filtered paths!")
                    bad_file_paths = [k for k in list(file_losses.keys()) if
                        file_losses[k]['avg_max_attention'] < hparams.min_avg_max_att or# if attention stength if too weak
                        file_losses[k]['att_diagonality']   > hparams.max_diagonality or# or diagonality is too high
                        file_losses[k]['spec_MSE']          > hparams.max_spec_mse]     # or audio quality is too low
                                                                                        # then add to bad files list
                    bad_file_paths = set(bad_file_paths)                                # and remove from dataset
                    filted_filelist = [x for x in train_loader.dataset.filelist if not (x[0] in bad_file_paths)]
                    train_loader.dataset.update_filelist(filted_filelist)
                    print(f"Done! {len(bad_file_paths)} Files removed from dataset. {len(filted_filelist)} Files remain.")
                    del filted_filelist, bad_file_paths
                    if iteration > hparams.speaker_mse_sampling_start:
                        print("Updating dataset with speaker MSE Sampler!")
                        if original_filelist is None:
                            original_filelist = train_loader.dataset.filelist
                        train_loader.dataset.update_filelist(get_mse_sampled_filelist(
                                                             original_filelist, file_losses, hparams.speaker_mse_exponent, seed=iteration))
                        print("Done!")
                
                # end of epoch loop
            training = False # exit the While loop
        
        #except Exception as ex: # print Exception and continue from checkpoint. (turns out it takes < 4 seconds to restart like this, fucking awesome)
        except LossExplosion as ex: # print Exception and continue from checkpoint. (turns out it takes < 4 seconds to restart like this, fucking awesome)
            print(ex) # print Loss
            checkpoint_path = os.path.join(args.output_directory, "best_val_model")
            assert os.path.exists(checkpoint_path), "best_val_model checkpoint must exist for automatic restarts"
            
            if hparams.fp16_run:
                amp._amp_state.loss_scalers[0]._loss_scale = 32768
            
            # clearing VRAM for load checkpoint
            model.zero_grad()
            x=y=y_pred=loss=len_loss=loss_z=loss_w=loss_s=loss_att=dur_loss_z=dur_loss_w=dur_loss_s=None
            torch.cuda.empty_cache()
            
            model.eval()
            model, optimizer, _learning_rate, iteration, best_validation_loss, saved_lookup = load_checkpoint(checkpoint_path, model, optimizer)
            learning_rate = optimizer.param_groups[0]['lr']
            epoch_offset = max(0, int(iteration / len(train_loader)))
            model.train()
            checkpoint_iter = iteration
            iteration += 1
            n_restarts += 1
        except KeyboardInterrupt as ex:
            print(ex)
            training = False
            raise KeyboardInterrupt()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-o', '--output_directory', type=str, default='outdir',
                        help='directory to save checkpoints')
    parser.add_argument('-l', '--log_directory', type=str, default='logdir',
                        help='directory to save tensorboard logs')
    parser.add_argument('-c', '--checkpoint_path', type=str, default=None,
                        required=False, help='checkpoint path')
    parser.add_argument('--warm_start', action='store_true',
                        help='load model weights only, ignore specified layers')
    parser.add_argument('--warm_start_force', action='store_true',
                        help='load model weights only, ignore all missing/non-matching layers')
    parser.add_argument('--detect_anomaly', action='store_true',
                        help='detects NaN/Infs in autograd backward pass and gives additional debug info.')
    parser.add_argument('--gen_mels', action='store_true',
                        help='Generate mel spectrograms. This will help reduce the memory required.')
    parser.add_argument('--n_gpus', type=int, default=1,
                        required=False, help='number of gpus')
    parser.add_argument('--rank', type=int, default=0,
                        required=False, help='rank of current gpu')
    parser.add_argument('--group_name', type=str, default='group_name',
                        required=False, help='Distributed group name')
    parser.add_argument('--hparams', type=str,
                        required=False, help='comma separated name=value pairs')
    
    args = parser.parse_args()
    hparams = create_hparams(args.hparams)
    
    torch.backends.cudnn.enabled   = hparams.cudnn_enabled
    torch.backends.cudnn.benchmark = hparams.cudnn_benchmark
    
    print("FP16 Run:", hparams.fp16_run)
    print("Dynamic Loss Scaling:", hparams.dynamic_loss_scaling)
    print("Distributed Run:", hparams.distributed_run)
    print("cuDNN Enabled:", hparams.cudnn_enabled)
    print("cuDNN Benchmark:", hparams.cudnn_benchmark)
    
    if args.gen_mels:
        print("Generating Mels...")
        create_mels(hparams)
        print("Finished Generating Mels")
    
    if args.detect_anomaly: # checks backprop for NaN/Infs and outputs very useful stack-trace. Runs slowly while enabled.
        torch.autograd.set_detect_anomaly(True)
        print("Autograd Anomaly Detection Enabled!\n(Code will run slower but backward pass will output useful info if crashing or NaN/inf values)")
    
    # these are needed for fp16 training, not inference
    if hparams.fp16_run:
        from apex import amp
    else:
        global amp
        amp = None
    try:
        from apex import optimizers as apexopt
    except:
        pass
    
    train(args, args.rank, args.group_name, hparams)

