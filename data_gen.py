import sys
sys.path.insert(0, "../meta_material_databank")
sys.path.insert(0, "../SASA")

from datetime import datetime
import random
import os
import pickle
import argparse
import numpy as np
import sqlite3
from sklearn.preprocessing import MultiLabelBinarizer
#self written modules
from crawler import Crawler
from stack import *
import train
#%%


def n_SiO2_formular(w):
    """
    Calculates the refractiv index of SiO2

    Parameters
    ----------
    w : vec
        wavelengths in micro meters

    Returns
    -------
    n : vec
        refractiv indeces
    """
    a1 = 0.6961663
    a2 = 0.4079426
    a3 = 0.8974794
    c1 = 0.0684043
    c2 = 0.1162414
    c3 = 9.896161
    n = np.sqrt(a1*w**2/(w**2 - c1**2) +
        a2*w**2/(w**2 - c2**2) + a3*w**2/(w**2 - c3**2) + 1)
    return n

def remove_equivalent_combinations(p1, p2):
    """
    There are different stacks which have the same spectral behaviour.
    In these cases the NN can't "decide" which option to pick. This function
    rearanges p1 and p2 so that only on of the equivalent stacks is possible.

    Parameters
    ----------
    p1 : dict
    p2 : dict

    Returns
    -------
    (p1, p2) or
    (p2, p1)
    """
    if p1["particle_material"] == "Al" and p2["particle_material"] == "Au":
        return p2, p1
    else:
        return p1, p2

def pick_training_layers(crawler, param_dict):
    """
    This needs to be generalised for arbitrary keys at some point
    """
    #choose random parameters
    layer1 = {}
    layer2 = {}

    for key, val in train.MODEL_DISCRETE_PREDICTIONS.items():
        l1 = random.choice(val)
        l2 = random.choice(val)

        #arange them unambiguously
        if l1 < l2:
            l1, l2 = l2, l1

        layer1[key] = l1
        layer2[key] = l2


    query1 = f"""SELECT simulations.m_file, simulations.adress
    FROM simulations
    INNER JOIN square
    ON simulations.simulation_id = square.simulation_id
    WHERE particle_material = '{layer1["particle_material"]}'
    AND square.hole = '{layer1["hole"]}'
    ORDER BY RANDOM()
    LIMIT 1"""

    query2 = f"""SELECT simulations.m_file, simulations.adress
    FROM simulations
    INNER JOIN square
    ON simulations.simulation_id = square.simulation_id
    WHERE particle_material = '{layer2["particle_material"]}'
    AND square.hole = '{layer2["hole"]}'
    ORDER BY RANDOM()
    LIMIT 1"""

    crawler.cursor.execute(query1)
    m_file, adress = crawler.cursor.fetchone()
    m1 = crawler.load_smat_npy(name=m_file, adress=adress)
    p1 = param_dict[m_file+adress+".npy"]

    crawler.cursor.execute(query2)
    m_file, adress = crawler.cursor.fetchone()
    m2 = crawler.load_smat_npy(name=m_file, adress=adress)
    p2 = param_dict[m_file+adress+".npy"]

    return m1 ,m2, p1, p2



def create_random_stack(crawler, param_dict):
    """
    Generates a random 2-Layer Stack and returns it's spectrum calculated via
	SASA and the generated parameters

    Parameters
    ----------
    samt1 : str
    smat2 : str
        these need to have the same
        wavelength_start/stop and spectral_points
	crawler : Crawler object

    Returns
    -------
    spectrum : array
    p1 : dict
        layer 1 parameters
    p2 : dict
        layer 2 parameters
    params : dict
        stack parameters

    """

    m1, m2, p1, p2 = pick_training_layers(crawler, param_dict)


    wav = np.linspace(0.5, 1.0, 128)
    SiO2 = n_SiO2_formular(wav)

    l1, l2 = MetaLayer(m1, SiO2, SiO2), MetaLayer(m2, SiO2, SiO2)

    phi = random.uniform(0,90)
    l1.rotate(phi)

    h = random.uniform(0.1, 0.3)
    spacer = NonMetaLayer(SiO2, height=h)

    s = Stack([l1, spacer, l2], wav, SiO2, SiO2)
    smat = s.build()
    spectrum = np.abs( smat[:, 2, 2] )**2 / SiO2

    p_stack = { 'angle' : phi,
               'spacer_height': h,
             }
    return spectrum, p1, p2, p_stack


def create_batch(size, mlb, crawler, param_dict):
    """Uses create_random_stack() to create a minibatch

    Parameters
    ----------
    size : int
           the batch size
    ids : list
          all these need to have the same
          wavelength_start/stop and spectral_points
    crawler : Crawler obj
    mlb : MultiLabelBinarizer obj
          initialized to the discrete labels

    Returns
    -------
    model_in : size x MODEL_INPUTS Array
    model_out : size x MODEL_OUTPUTS Array

    """


    model_in = np.zeros((size, train.MODEL_INPUTS))
    labels1 = []
    labels2 = []
    stack_params = []

    for i in range(size):

        #generate stacks until one doesn't block all incomming light
        while True:
            spectrum, p1, p2, p_stack = create_random_stack(crawler, param_dict)

            if np.max(spectrum) > 0.1:
                break

        #save the input spectrum
        model_in[i] = spectrum

        #save the layer parameters which led to the spectrum
        label1 = [p1[key].strip() for key in train.MODEL_DISCRETE_PREDICTIONS]
        label2 = [p2[key].strip() for key in train.MODEL_DISCRETE_PREDICTIONS]

        labels1.append(label1)
        labels2.append(label2)

        stack_params.append((p1, p2, p_stack))

    #encode the labels
    enc1 = mlb.fit_transform(labels1)
    enc2 = mlb.fit_transform(labels2)

    model_out = np.concatenate((enc1, enc2), axis=1)

    return model_in, model_out, stack_params

def LabelBinarizer():
    discrete_params = ['Au', 'Al', 'holes', 'no holes']
    mlb = MultiLabelBinarizer(classes=np.array(discrete_params, dtype=object))
    mlb.fit_transform([['Au', 'holes']])
    return mlb

#%%
if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--smat-directory", default="data/smat_data",
    	help="path to input directory containing .npy files")
    ap.add_argument("-p", "--params", default="data/params.pickle",
    	help="path to the .pickle file containing the smat parameters")
    ap.add_argument("-n", "--number-of-batches", default=10, type=int)
    ap.add_argument("-b", "--batch-dir", default="data/batches",
    	help="path to output batch directory")
    args = vars(ap.parse_args())


    print("[INFO] connecting to db...")
    with sqlite3.connect(database="/home/tim/Uni/BA/meta_material_databank/NN_smats.db") as conn:
        crawler = Crawler(
            directory="data/smat_data",
            cursor=conn.cursor()
        )


    print("[INFO] loading data...")
    lb = LabelBinarizer()

    with open(args["params"], "rb") as f:
        param_dict = pickle.load(f)

    for i in range(args["number_of_batches"]):
        print(f"[INFO] creating batch {i+1}/{args['number_of_batches']}")
        x, y, stack_params = create_batch(train.BATCH_SIZE, lb, crawler, param_dict)
        ts = str(datetime.now()).replace(" ", "_")
        np.save(f"{args['batch_dir']}/input/{ts}.npy", x)
        np.save(f"{args['batch_dir']}/discrete_out/{ts}.npy", y)

        with open(f"{args['batch_dir']}/params/{ts}.pickle", "wb") as f:
            pickle.dump(stack_params, f)
