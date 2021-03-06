import StringIO
import csv
import sys
import pdb 
import string 
import math 
from collections import defaultdict 
import re 

import numpy as np 

from nltk import word_tokenize

import pandas as pd 

import sklearn 
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cross_validation import KFold
from sklearn.grid_search import GridSearchCV
from sklearn.linear_model import SGDClassifier 
from sklearn.cross_validation import StratifiedShuffleSplit
from sklearn.svm import SVC

import pyanno
from pyanno.annotations import AnnotationsContainer
from pyanno.models import ModelB # see http://docs.enthought.com/uchicago-pyanno/pyanno.models.html#pyanno.modelB.ModelB

import annotator_rationales as ar


STOP_WORDS = [l.replace("\n", "") for l in open("pubmed.stoplist", 'rU').readlines()]
HEADERS = ['workerId', 'experimentId', 'hitId', 'documentId', 'q1', 'q2', 'q3', 'q4', 'q1keys', 'q2keys', 'q3keys', 'q4keys', 'q1cust', 'q2cust', 'q3cust', 'q4cust', 'q1_norationales_expl', 'q2_norationales_expl', 'q3_norationales_expl', 'q4_norationales_expl', 'q1_norationales_reverse', 'q2_norationales_reverse', 'q3_norationales_reverse', 'q4_norationales_reverse', 'comments', 'honeypotId', 'honeypotPass', 'qualificationTest', 'timeUsed', 'ts']

def load_protonbeam_annotations(annotations_path="fullscale-data/protonbeam.csv"):
    annotations = pd.read_csv("fullscale-data/protonbeam.csv", delimiter="\t", header=None)
    annotations.columns = HEADERS
    return annotations

def load_texts_and_pmids(citations_and_labels_path="fullscale-data/protonbeam_data.csv"):
    protonbeam = pd.read_csv(citations_and_labels_path)
    texts = []
    for title, abstract in zip(protonbeam["title"].values, protonbeam["abstract"].values):
        # this means the title is missing (well, nan, which is a float)
        if isinstance(title, float): 
            title_tokens = []
        else:
            title_tokens =  word_tokenize(title.decode('utf-8'))

        abstract_tokens = word_tokenize(abstract.decode('utf-8'))
        ### 
        # not differentiating between titles and abstracts for now, 
        # or possibly ever, because this complicates the rationales
        # learning thing.

        #cur_text = ["TITLE"+t for t in title_tokens if t not in STOP_WORDS]
        cur_text = title_tokens
        cur_text.extend(abstract_tokens)

        texts.append(" ".join(cur_text))


    return texts, protonbeam["pmid"].values


def read_lbls(labels_path="fullscale-data/protonbeam_labels.csv"):
    lbls = pd.read_csv(labels_path)
    # all of these pmids were screened in at the citation level.
    #lbls["abstrackr_decision"]
    lvl1_set = lbls[lbls["lvl1"].isin(["yes", "Yes"])]["PMID"].values

    lvl2_set = lbls[lbls["lvl2"].isin(["yes", "Yes"])]["PMID"].values
    return lvl1_set, lvl2_set

# do we need pmids? because we lose them here!
def flatten_rationales(all_rationales, workers):
    # s is something like 
    #   'describe a 24-year-old Pakistani man,"admitted twice to our hospital"'
    # here we parse this CSV string
    rationales_flat = []
    workers_extended = []
    for i,s in enumerate(all_rationales):
        cur_rationales = csv.reader(StringIO.StringIO(s)).next()
        rationales_flat.extend(cur_rationales)
        workers_extended.extend([workers[i]]*len(cur_rationales))

    return rationales_flat, workers_extended


def get_M_overall(annotations, train_pmids):
    rows_list = []

    for pmid in train_pmids:
        all_annotations_for_pmid = annotations[annotations['documentId'] == pmid]
        for worker, question_answers in all_annotations_for_pmid.groupby("workerId"):
            question_answers_txt = question_answers[['q1', 'q2', 'q3']].values[0]
            question_answer_num = question_answers[['q4']].values[0][0]
            final_answer = 3 if (
                    "No" in question_answers_txt or "\\N" in question_answers_txt or (
                    question_answer_num == '\\N' or 
                    (question_answer_num != 'NoInfo' and question_answer_num < 10))) else 4
            row_d = {"workerId":worker, "label":final_answer, "documentId":pmid}
            rows_list.append(row_d)

    doc_annos = pd.DataFrame(rows_list)
    #unique_workers = list(set(doc_annos["workerId"].values))
    #pdb.set_trace()
    pivoted = doc_annos.pivot(index="documentId", columns="workerId")
    pivoted = pivoted.fillna(2)
    m = pd.DataFrame.as_matrix(pivoted)    
    workers = list(pivoted['label'].keys())
    return m, workers 
    

def estimate_quality_instance_level(annotations, pmids):
    m, workers = get_M_overall(annotations, pmids)
    instance_model = ModelB.create_initial_state(2, len(workers))
    anno = AnnotationsContainer.from_array(m, missing_values=[2])
    instance_model.map(anno.annotations) 
    proxy_skill = (instance_model.theta[:,0,0] + instance_model.theta[:,1,1]) / 2.0
    return dict(zip(workers, proxy_skill))

def get_M_q(data, qnum, pmids=None):
    '''
    returns an |pmids| x |workers| matrix, where 
    columns are worker responses; also provides 
    list that maps worker ids to columns. 
    '''

    q_annotations = annotations = data[["q%s"%qnum, "documentId", "workerId"]]
    if pmids is not None:
        q_annotations = q_annotations[q_annotations['documentId'].isin(pmids)]

    pivoted = q_annotations.pivot(index="documentId", columns="workerId")
    '''
    we use these kind of wacky labels because the pyanno library
    seems to prefer integers...  
    '''
    #pivoted.replace("CantTell", 4, inplace=True)
    pivoted.replace(["Yes", "yes","CantTell"], 4, inplace=True)
    pivoted.replace(["No", "no"], 3, inplace=True)
    # we use '2' as our missing value; this is later signaled 
    # to the AnnotationsContainer
    pivoted.replace(["\\N","NA"], np.nan, inplace=True)
    pivoted = pivoted.fillna(2)
    workers = list(pivoted["q%s"%qnum].keys()) # this preserves order
    # matrix 
    m = pd.DataFrame.as_matrix(pivoted["q%s"%qnum])

    return m, workers

def estimate_quality_for_q(annotations, qnum, pmids=None):
    m, workers = get_M_q(annotations, qnum, pmids=pmids)
    q_model = ModelB.create_initial_state(2, len(workers))
    anno = AnnotationsContainer.from_array(m, missing_values=[2])
    
    q_model.map(anno.annotations)
    
    '''
    pi[k] is the probability of label k
    theta[j,k,k'] is the probability that 
        annotator j reports label k' for an 
        item whose real label is k, i.e. 
        P( annotator j chooses k' | real label = k)
    '''
    # this is a simple mean of sensitivity and specificity
    # @TODO revisit? 
    proxy_skill = (q_model.theta[:,0,0] + q_model.theta[:,1,1]) / 2.0
    return dict(zip(workers, proxy_skill))


def get_q_rationales(data, qnum, pmids=None):
    pos_annotations_for_q = data[data["q%s"%qnum]=="Yes"]
    neg_annotations_for_q = data[data["q%s"%qnum]=="No"]

    if pmids is not None:
        # then only include those rationales associated with pmids of 
        # interest
        pos_annotations_for_q = \
            pos_annotations_for_q[pos_annotations_for_q['documentId'].isin(pmids)]

        neg_annotations_for_q = \
            neg_annotations_for_q[neg_annotations_for_q['documentId'].isin(pmids)]
        
    
    pos_rationales = pos_annotations_for_q["q%skeys" % qnum].values
    pos_worker_ids = pos_annotations_for_q["workerId"].values

    def _quick_clean(s): 
        exclude = set(string.punctuation)
        s = re.sub("\d+", "", s) # scrub digits
        s = s.lower().strip()
        s = ''.join(ch for ch in s if ch not in exclude)
        return s 

    # collapse into a single set
    pos_rationales, pos_worker_ids = flatten_rationales(pos_rationales, pos_worker_ids)
    pos_rationales = [_quick_clean(pr) for pr in pos_rationales]
    #pos_rationales = list(chain.from_iterable(pos_rationales))
    
    neg_rationales = neg_annotations_for_q["q%skeys" % qnum].values
    neg_rationales = [_quick_clean(nr) for nr in neg_rationales]

    neg_worker_ids = neg_annotations_for_q["workerId"].values

    #neg_rationales = list(chain.from_iterable(neg_rationales))
    neg_rationales, neg_worker_ids = flatten_rationales(neg_rationales, neg_worker_ids)


    ### do we need these??
    # get pubmids
    #pos_pmids = pos_annotations_for_q["documentId"]
    #neg_pmids = neg_annotations_for_q["documentId"]

    # collapse into a single set; note that this is basically
    # the most naive means of combining rationales

    #pdb.set_trace()
    pos_rationales_to_worker_ids = defaultdict(list)
    for pos_rationale, pos_worker in zip(pos_rationales, pos_worker_ids):
        pos_rationales_to_worker_ids[pos_rationale].append(pos_worker)

    neg_rationales_to_worker_ids = defaultdict(list)
    for neg_rationale, neg_worker in zip(neg_rationales, neg_worker_ids):
        neg_rationales_to_worker_ids[neg_rationale].append(neg_worker)
    

    #return list(set(pos_rationales)), list(set(neg_rationales))

    # @TODO should probably roll up into an object
    #return pos_rationales, pos_worker_ids, neg_rationales, neg_worker_ids
    return pos_rationales_to_worker_ids, neg_rationales_to_worker_ids

'''
def get_q_rationales(data, qnum, pmids=None):
    pos_annotations_for_q = data[data["q%s"%qnum]=="Yes"]
    neg_annotations_for_q = data[data["q%s"%qnum]=="No"]

    if pmids is not None:
        # then only include those rationales associated with pmids of 
        # interest
        pos_annotations_for_q = \
            pos_annotations_for_q[pos_annotations_for_q['documentId'].isin(pmids)]

        neg_annotations_for_q = \
            neg_annotations_for_q[neg_annotations_for_q['documentId'].isin(pmids)]
        
    
    pos_rationales = pos_annotations_for_q["q%skeys" % qnum].values
    # collapse into a single set
    pos_rationales = flatten_rationales(pos_rationales)

    #pos_rationales = list(chain.from_iterable(pos_rationales))
    
    neg_rationales = neg_annotations_for_q["q%skeys" % qnum].values
    #neg_rationales = list(chain.from_iterable(neg_rationales))
    neg_rationales = flatten_rationales(neg_rationales)

    ### do we need these??
    # get pubmids
    #pos_pmids = pos_annotations_for_q["documentId"]
    #neg_pmids = neg_annotations_for_q["documentId"]

    # collapse into a single set; note that this is basically
    # the most naive means of combining rationales
    return list(set(pos_rationales)), list(set(neg_rationales))
'''

def get_SGD(class_weight="auto", loss="log", random_state=None, fit_params=None, n_jobs=1):
    #C_range = np.logspace(-2, 10, 13)
    #return SGDClassifier(penalty=None)#, class_weight="auto")
    params_d = {"alpha": 10.0**-np.arange(0,7)}
    
    q_model = SGDClassifier(class_weight=class_weight, loss=loss, random_state=random_state, n_jobs=n_jobs)

    clf = GridSearchCV(q_model, params_d, scoring='f1', fit_params=fit_params, n_jobs=n_jobs)
    return clf 

def get_svm(y, n_jobs=1):
    C_range = np.logspace(-2, 10, 13)
    gamma_range = np.logspace(-9, 3, 13)
    param_grid = dict(gamma=gamma_range, C=C_range)
    cv = StratifiedShuffleSplit(y, n_iter=5, test_size=0.2, random_state=42)
    clf = GridSearchCV(SVC(class_weight="auto"), param_grid=param_grid, cv=cv, scoring="f1", n_jobs=n_jobs)

    return clf


def cartesian(arrays, out=None):
    """
    Generate a cartesian product of input arrays.

    Parameters
    ----------
    arrays : list of array-like
        1-D arrays to form the cartesian product of.
    out : ndarray
        Array to place the cartesian product in.

    Returns
    -------
    out : ndarray
        2-D array of shape (M, len(arrays)) containing cartesian products
        formed of input arrays.

    Examples
    --------
    >>> cartesian(([1, 2, 3], [4, 5], [6, 7]))
    array([[1, 4, 6],
           [1, 4, 7],
           [1, 5, 6],
           [1, 5, 7],
           [2, 4, 6],
           [2, 4, 7],
           [2, 5, 6],
           [2, 5, 7],
           [3, 4, 6],
           [3, 4, 7],
           [3, 5, 6],
           [3, 5, 7]])

    """

    arrays = [np.asarray(x) for x in arrays]
    dtype = arrays[0].dtype

    n = np.prod([x.size for x in arrays])
    if out is None:
        out = np.zeros([n, len(arrays)], dtype=dtype)

    m = n / arrays[0].size
    out[:,0] = np.repeat(arrays[0], m)
    if arrays[1:]:
        cartesian(arrays[1:], out=out[0:m,1:])
        for j in xrange(1, arrays[0].size):
            out[j*m:(j+1)*m,1:] = out[0:m,1:]
    return out



def rationales_exp_all_train(model="cf-stacked", use_worker_qualities=False, n_jobs=1):
    ##
    # basics: just load in the data + labels, vectorize
    annotations = load_protonbeam_annotations()
    lvl1_pmids, lvl2_pmids = read_lbls()
    # we'll use all crowd annotated data as training.
    train_pmids = list(set(annotations['documentId'].values))
    texts, pmids = load_texts_and_pmids()
    train_indices, test_indices = [], []
    train_y, test_y = [], []
    train_worker_ids = [] # for grouped
    answers_for_train_pmids = []

    for i, pmid in enumerate(pmids):    

        if pmid in train_pmids:
            '''
            train_indices.append(i) 
            if pmid in lvl1_pmids:
                train_y.append(1)
            else: 
                train_y.append(-1)
            '''
            
            '''
            figure out each label 
            '''

            q_decisions_for_pmid = annotations[annotations['documentId'] == pmid]
            for worker, question_answers in q_decisions_for_pmid.groupby("workerId"):
                # calculate the 'effective' label given by this worker,
                # as a function of their question decisions

                if model == "cf-independent-responses":
                    q1 = question_answers[['q1']].values[0]
                    q2 = question_answers[['q2']].values[0]
                    q3 = question_answers[['q3']].values[0]
                    q4 = question_answers[['q4']].values[0][0]
                    q1a = -1 if (q1 == "No" or q1 == "\\N") else 1
                    q2a = -1 if (q2 == "No" or q2 == "\\N") else 1
                    q3a = -1 if (q3 == "No" or q3 == "\\N") else 1
                    q4a = -1 if (q4 == '\\N' or (q4 != 'NoInfo' and q4 < 10)) else 1
                    train_y.append(q1a)
                    train_indices.append(i) # repeat the instance
                    train_y.append(q2a)
                    train_indices.append(i) # repeat the instance
                    train_y.append(q3a)
                    train_indices.append(i) # repeat the instance
                    train_y.append(q4a)
                    train_indices.append(i) # repeat the instance
                else:
                    question_answers_txt = question_answers[['q1', 'q2', 'q3']].values[0]
                    question_answer_num = question_answers[['q4']].values[0][0]
                    final_answer = -1 if ("No" in question_answers_txt or "\\N" in question_answers_txt or (question_answer_num == '\\N' or (question_answer_num != 'NoInfo' and question_answer_num < 10))) else 1
                    train_y.append(final_answer)
                    train_indices.append(i) # repeat the instance

                train_worker_ids.append(worker)

                q_fv = np.zeros(3)#np.zeros(3*3) # unidentifiable if we have an intercept!

                #for q_index, qa in enumerate(question_answers):
                for q_index, q_str in enumerate(["q1", "q2", "q3"]):
                    qa = question_answers[q_str].values[0]
                    # so would expect both to be negative
                    # weights; errr possibly the missing
                    # indicator could be slightly positive
                    # as slight correction
                    if qa == "\\N":
                        q_fv[q_index] = .5 # unknown?
                        #pass
                        # q_fv[3*q_index+2] = 0#1.0 # missing indicator
                    else:
                        #if qa in ("No", "no"):
                        #    q_fv[3*q_index] = 1.0
                        if qa in ("Yes", "yes"):
                            q_fv[q_index] = 1.0
                #pdb.set_trace()
                answers_for_train_pmids.append(q_fv)
        else:    
            ###
            # bcw: note! we are *training* with respect
            # to level-1 labels, but then *testing* with
            # respect to level-2, which in practice makes
            # sense, but might seem a little odd to others.
            # in the past, we've trained and tested on
            # just level 1 to keep things simple. 
            test_indices.append(i)
            # if pmid in lvl2_pmids:
            if pmid in lvl1_pmids:
                test_y.append(1)
            else:
                test_y.append(-1)
    
    vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1,2), min_df=3, max_features=50000)
    
    X_all = vectorizer.fit_transform(texts)

    X_train = X_all[train_indices]
    X_test = X_all[test_indices]
    #pdb.set_trace()
    test_y = np.array(test_y)
    train_y = np.array(train_y)
    
    if "cf" in model:
        if model == "cf-stacked":
            # TODO: Fix the occasional zero division error (no true or false positives in the results?)
            # Traceback (most recent call last):
            # File "<stdin>", line 1, in <module>
            # File "proton_beam.py", line 544, in rationales_exp_all_train
            # sensitivity, specificity, f= ar.compute_measures(tp, fp, fn, tn)
            # File "annotator_rationales.py", line 436, in compute_measures
            # precision   = tp / (tp + fp)
            # ZeroDivisionError: float division by zero
            #
            q_models = get_q_models(annotations, X_all, pmids, train_pmids,
                                    vectorizer, model=model,
                                    use_worker_qualities=use_worker_qualities,
                                    use_rationales=False,
                                    n_jobs=n_jobs)
            q_train = np.matrix([np.array(q_m.predict_proba(X_all[train_indices]))[:,1] for q_m in q_models]).T
            #q_train = np.matrix([np.array(q_m.decision_function(X[train_indices])) for q_m in q_models]).T
            #m = get_svm(train_y)
            m = get_SGD(class_weight=None, random_state=42, n_jobs=n_jobs)

            print "fittting stacked model... "
            m.fit(q_train, train_y)

            # so this is a matrix 3 columns of predictions; one per question
            # #of rows = # of test citations

            q_predictions = np.matrix([np.array(q_m.predict_proba(X_all[test_indices])[:,1]) for q_m in q_models]).T
            #q_predictions = np.matrix([np.array(q_m.decision_function(X[test_indices])) for q_m in q_models]).T
            aggregate_predictions = m.predict(q_predictions)
        elif model == "cf-predictions":
            # TODO(byron.wallace@utexas.edu): Please check this code is up to date. It's from an earlier revision.
            # TODO(byron.wallace@utexas.edu): Also maybe a better name? cf-predictions was.. lazy.. sorry ;-)
            q_models = get_q_models(annotations, X_all, pmids, train_pmids,
                                vectorizer, model=model,
                                use_worker_qualities=use_worker_qualities,
                                use_rationales=False,
                                n_jobs=n_jobs)

            q_train = np.matrix([np.array(q_m.predict_proba(X_train))[:,1] for q_m in q_models]).T

            #q1_preds =  q_models[0].predict(X_test) #np.matrix([q_m.predict(X_train) for q_m in q_models]).T
            #aggregate_predictions = q1_preds


            params_d = {"alpha": 10.0**-np.arange(0,7)}
            #class_weight="auto",  further boosts sensitivity...
            q_model = SGDClassifier(class_weight="auto", loss="hinge", random_state=42, n_jobs=n_jobs)
            m = GridSearchCV(q_model, params_d, scoring='f1', n_jobs=n_jobs)

            #m = get_SGD()
            print "fittting predictions model... "
            #pdb.set_trace()

            # do not use expert labels in training!!! FOR EITHER
            # MODEL
            '''
            And now for extremely naive/slow model fitting!
            '''
            '''
            lambda_ = 1
            alpha_vals = np.linspace(.2,.8,25)
            beta_vals = np.linspace(.2,.8,25)
            gamma_vals = np.linspace(.1,.8,25)
            a_star, b_star, g_star = None, None, None
            best_score, best_sens, best_spec = -np.inf, -np.inf, -np.inf
            for a, b, g in cartesian([alpha_vals, beta_vals, gamma_vals]):
                #preds = ((q_train[:,0] > a) & ((q_train[:,1] > b) | (q_train[:,2] > g)))
                preds = ((q_train[:,0] > a) & (q_train[:,1] > b) & (q_train[:,2] > g))
                #preds = q_train[:,0] > a
                preds = np.array(map(lambda x: 1 if x else -1, preds))
                sens = sklearn.metrics.accuracy_score(train_y[train_y>0], preds[train_y>0])
                spec = sklearn.metrics.accuracy_score(train_y[train_y<=0], preds[train_y<=0])

                cur_score = lambda_ * sens + spec

                if cur_score > best_score:
                    a_star, b_star, g_star = a, b, g
                    best_score = cur_score
                    best_sens = sens
                    best_spec = spec

            '''

            #pdb.set_trace()

            # so this is a matrix 3 columns of predictions; one per question
            # #of rows = # of test citations
            q_predictions = np.matrix([np.array(q_m.predict_proba(X_test)[:,1]) for q_m in q_models]).T
            #pdb.set_trace()
            #q_predictions = np.matrix([np.array(q_m.predict(X_test)) for q_m in q_models]).T

            # stacking aggregation
            #m.fit(q_train, train_y)
            #aggregate_predictions = m.predict(q_predictions)

            #q_predictions = np.matrix([np.array(q_m.decision_function(X[test_indices])) for q_m in q_models]).T

            # this is the OR approach for q's 2&3
            #aggregate_predictions = ((q_predictions[:,0] > a_star) & ((q_predictions[:,1] > b_star) | (q_predictions[:,2] > g_star)))

            # standard AND aggregation
            #aggregate_predictions = ((q_predictions[:,0] > a_star) & (q_predictions[:,1] > b_star) & (q_predictions[:,2] > g_star))
            #aggregate_predictions = np.array(map(lambda x: 1 if x else -1, aggregate_predictions ))
            #aggregate_predictions = ((q_predictions[:,0] >= .5) & (
            #                            q_predictions[:,1] >= .5) & (q_predictions[:,2] >= .5))
            #aggregate_predictions = ((q_predictions[:,0] > 0) &
            #                           (q_predictions[:,1] > 0) & (q_predictions[:,2] >= 0))

            #aggregate_predictions = (q_predictions[:,0] + q_predictions[:,1] + q_predictions[:,2]) >= 3
            #pdb.set_trace()
            aggregate_predictions = (q_predictions[:,0] >= .1) & ((q_predictions[:,1] >= .5) | (q_predictions[:,2] >= .5))
            aggregate_predictions = np.array(map(lambda x: 1 if x else -1, aggregate_predictions ))

            #
            #pdb.set_trace()
        elif model == "cf-responses-as-features" or model == "cf-responses-as-features-wr":
            if "wr" in model:
                q_models = get_q_models(annotations, X_all, pmids, train_pmids,
                                        vectorizer, model=model,
                                        use_worker_qualities=use_worker_qualities,
                                        use_rationales=True,
                                        n_jobs=n_jobs)
            else:
                q_models = get_q_models(annotations, X_all, pmids, train_pmids,
                                        vectorizer, model=model,
                                        use_worker_qualities=use_worker_qualities,
                                        use_rationales=False,
                                        n_jobs=n_jobs)

            # we train on the predicted probabilities, rather than the observed labels, 
            # to sort of calibrate. 
            q_train = np.matrix([np.array(q_m.predict_proba(X_train))[:,1] for q_m in q_models]).T

            # bcw: introducing interaction features, too (9/29)
            # NOTE this seems to increase sens. at the expense of
            # a drop in spec. 
            # might also try adding three-level interaction feature!
            train_q_fvs = np.zeros((X_train.shape[0], 4))

            train_q_fvs[:,0] = q_train[:,0].T
            train_q_fvs[:,1] = q_train[:,1].T
            train_q_fvs[:,2] = q_train[:,2].T

            ### 9/28
            train_q_fvs[:,3] = np.multiply(q_train[:,0], q_train[:,1]).T
            # 3-way interaction feature
            train_q_fvs[:,3] = np.multiply(train_q_fvs[:,3], q_train[:,2].T)

            #train_q_fvs[:,4] = np.multiply(q_train[:,0], q_train[:,2]).T

            # also introduce

            print "fittting responses-as-features model... "


            #pdb.set_trace()

            # so this is a matrix 3 columns of predictions; one per question
            # #of rows = # of test citations
            q_predictions = np.matrix([np.array(q_m.predict_proba(X_test)[:,1]) for q_m in q_models]).T
            #pdb.set_trace()
            #pdb.set_trace()
            #q_predictions = np.matrix([np.array(q_m.predict_proba(X_test)) for q_m in q_models]).T

            # stacking aggregation
            #m.fit(q_train, train_y)
            #aggregate_predictions = m.predict(q_predictions)



            test_q_fvs = np.zeros((X_test.shape[0], 4)) # was 3.
            #pdb.set_trace()
            test_q_fvs[:,0] = q_predictions[:,0].T
            test_q_fvs[:,1] = q_predictions[:,1].T
            #test_q_fvs[:,3] = 1-q_predictions[:,1].T
            test_q_fvs[:,2] = q_predictions[:,2].T
            #test_q_fvs[:,6] = 1-q_predictions[:,2].T
            #test_q_fvs[:,7] = q_predictions[:,2].T


            # bcw: interaction features (9/28)
            test_q_fvs[:,3] = np.multiply(q_predictions[:,0], q_predictions[:,1]).T
            #pdb.set_trace()
            test_q_fvs[:,3] = np.multiply(test_q_fvs[:,3], q_predictions[:,2].T)
            #test_q_fvs[:,4] = np.multiply(q_predictions[:,0], q_predictions[:,2]).T

            # populate test

            '''
            for q_index, qa in enumerate(question_answers):
                # so would expect both to be negative
                # weights; errr possibly the missing
                # indicator could be slightly positive
                # as slight correction
                if qa == "\\N":
                    fv[q_index*3+2] = 1.0 # missing indicator
                elif qa in ("No", "no"):
                    fv[q_index*3] = 1.0
                else:
                    fv[q_index*3+1] = 1.0
            '''

            m = get_SGD(loss="hinge", random_state=42, n_jobs=n_jobs)

            qa_matrix = np.matrix(answers_for_train_pmids)
            # augment X_train with question features?
            # this is really inefficient!
           
            #X_train_new = np.concatenate((X_train.todense(), qa_matrix), axis=1)
            X_train_new = np.concatenate((X_train.todense(), train_q_fvs), axis=1)

            #m.fit(X_train_new, train_y)
            m.fit(X_train_new, train_y)
            #m.fit(X[train_indices], train_y)
            #pdb.set_trace()

            X_test_new = np.concatenate((X_test.todense(), test_q_fvs), axis=1)
            #pdb.set_trace()
            aggregate_predictions = m.predict(X_test_new)
            #aggregate_predictions = m.predict(X_test)
        elif model == "cf-independent-responses":
            m = get_SGD(loss="hinge", random_state=42, n_jobs=n_jobs)
            m.fit(X_train, train_y)
            #m.fit(X[train_indices], train_y)
            aggregate_predictions = m.predict(X_test)
        else:
            raise NotImplementedError('No such method exists.')
    elif "grouped" in model:
        if model == "grouped":
            # grouped model; simpler case
            
            if use_worker_qualities:
                instance_quality_d = estimate_quality_instance_level(annotations, train_pmids)#get_M_overall(annotations, train_pmids)
                worker_weights = [instance_quality_d[w] for w in train_worker_ids]
                m = get_SGD(loss="hinge", random_state=42, fit_params={"sample_weight":worker_weights}, n_jobs=n_jobs)
                #pdb.set_trace()
                m.fit(X_train, train_y)
            else:
                m = get_SGD(loss="hinge", random_state=42, n_jobs=n_jobs)
                m.fit(X_train, train_y)
            #m.fit(X[train_indices], train_y)
            aggregate_predictions = m.predict(X_test)
        elif model == "grouped-wr":
            # grouped *with rationales* 
            m = get_grouped_rationales_model(
                annotations, X_all, train_y, pmids, 
                train_pmids, train_indices, vectorizer, 
                use_worker_qualities=use_worker_qualities,
                n_jobs=n_jobs)
            
            aggregate_predictions = m.predict(X_test)
        else:
            raise NotImplementedError('No such method exists.')
    else:
        raise NotImplementedError('No such method exists.')
    
    cm = sklearn.metrics.confusion_matrix(test_y, aggregate_predictions).flatten()
    #pdb.set_trace()
    tn, fp, fn, tp = cm 

    # tp, fp, fn, tn
    #sensitivity, specificity, f = ar.compute_measures(*cm / float(n_folds))
    sensitivity, specificity, precision, f2measure = ar.compute_measures(tp, fp, fn, tn)

    print "results on test set for model: %s." % model 
    print "using worker quality estimates? %s" % use_worker_qualities
    print "\n----" 

    print "tn, fp, fn, tp"
    print cm
    print "sensitivity: %s" % sensitivity
    print "specificity: %s" % specificity
    print "precision: %s" % precision
    # not the traditional F; we use spec instead 
    # of precision!
    print "F2: %s" % f2measure 
    print "----"

# def rationales_exp(model="ar", n_folds=5, use_worker_qualities=False):
#     '''
#     model options:
#         "ar"         -- annotators rationales model
#         "baseline"   -- baseline model, builds separate classifiers
#                         for each question
#         "grouped"    -- builds a single model, ignores questions
#         "grouped-ar" -- builds a single model *and* uses rationales, but
#                             groups them together.
#
#     '''
#     assert model in ("ar", "baseline", "grouped", "grouped-ar")
#
#     ##
#     # basics: just load in the data + labels, vectorize
#     annotations = load_protonbeam_annotations()
#     texts, pmids = load_texts_and_pmids()
#     vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1,2), min_df=3, max_features=50000)
#     # note that X and pmids will be aligned.
#     X = vectorizer.fit_transform(texts)
#
#
#     # these are sets of pmids that indicate positive instances;
#     # all other instances are negative. these are final, abstract
#     # level decisions (i.e., aggregate over the sub-questions)
#     lvl1_pmids, lvl2_pmids = read_lbls()
#
#     ###
#     # now generate folds
#     unique_labeled_pmids = list(set(annotations['documentId']))
#     folds = KFold(len(unique_labeled_pmids), n_folds=n_folds, random_state=10)
#
#     cm = np.zeros(4)
#     for train_indices, test_indices in folds:
#         train_pmids = np.array(unique_labeled_pmids)[train_indices].tolist()
#         test_pmids  = np.array(unique_labeled_pmids)[test_indices].tolist()
#         train_y, test_y = [], []
#         for pmid in test_pmids:
#             lbl = 1 if pmid in lvl1_pmids else -1
#             test_y.append(lbl)
#         test_y = np.array(test_y)
#
#         for pmid in train_pmids:
#             lbl = 1 if pmid in lvl1_pmids else -1
#             train_y.append(lbl)
#         train_y = np.array(train_y)
#
#         if not "grouped" in model:
#             q_models = get_q_models(annotations, X, pmids, train_pmids,
#                                     vectorizer, model=model,
#                                     use_worker_qualities=use_worker_qualities,
#                                     use_rationales=False)
#             q_train = np.matrix([np.array(q_m.predict_proba(X[train_indices]))[:,1] for q_m in q_models]).T
#             #q_train = np.matrix([np.array(q_m.decision_function(X[train_indices])) for q_m in q_models]).T
#             #m = get_svm(train_y)
#             m = get_SGD()
#
#             print "fittting stacked model... "
#             m.fit(q_train, train_y)
#
#             # so this is a matrix 3 columns of predictions; one per question
#             # #of rows = # of test citations
#
#             q_predictions = np.matrix([np.array(q_m.predict_proba(X[test_indices])[:,1]) for q_m in q_models]).T
#             #q_predictions = np.matrix([np.array(q_m.decision_function(X[test_indices])) for q_m in q_models]).T
#             aggregate_predictions = m.predict(q_predictions)
#
#         else:
#             if model == "grouped":
#                 # grouped model; simpler case
#                 m = get_SGD()
#                 m.fit(X[train_indices], train_y)
#
#                 aggregate_predictions = m.predict(X[test_indices])
#             else:
#                 # grouped *with rationales*
#                 m = get_grouped_rationales_model(
#                     annotations, X, train_y, pmids, train_pmids, train_indices, vectorizer, use_worker_qualities=use_worker_qualities)
#
#                 aggregate_predictions = m.predict(X[test_indices])
#
#         # stack these in a simple logistic
#
#         #col_aggregates = np.array(np.sum(q_predictions, axis=1)>0).astype(np.integer)
#         #col_aggregates[col_aggregates<1]=-1
#         #col_aggregates = col_aggregates[:,0]
#
#         cm += sklearn.metrics.confusion_matrix(test_y, aggregate_predictions).flatten()
#
#
#
#     tn, fp, fn, tp = cm #/ float(n_folds)
#     #pdb.set_trace()
#     # tp, fp, fn, tn
#     #sensitivity, specificity, f = ar.compute_measures(*cm / float(n_folds))
#     sensitivity, specificity, f= ar.compute_measures(tp, fp, fn, tn)
#
#
#     print "average results for model: %s." % model
#     print "using worker quality estimates? %s" % use_worker_qualities
#     print "\n----"
#     print "raw cm %s" % cm
#     print "average cm: \n"
#
#     #print "tp, fp, fn, tn"
#     print "tn, fp, fn, tp"
#     print cm/float(n_folds)
#     print "sensitivity: %s" % sensitivity
#     print "specificity: %s" % specificity
#     # not the traditional F; we use spec instead
#     # of precision!
#     print "F: %s" % f
#     print "----"

def get_unique(rationales_d, worker_qualities):
    unique_ids, unique_rationales = [], []
    for rationale, workers in rationales_d.items():
        cur_qualities = [worker_qualities[w] for w in workers]
        best_worker = workers[np.argmax(cur_qualities)]
        unique_ids.append(best_worker)
        unique_rationales.append(rationale)

    return unique_ids, unique_rationales


def get_grouped_rationales_model(annotations, X, train_y, pmids, train_pmids, train_indices, vectorizer, use_worker_qualities=True, n_jobs=1):
    pos_rationales_d, neg_rationales_d = defaultdict(list), defaultdict(list)
    overall_worker_quality_d = defaultdict(list)

    ### note that q2 is an integer (population size..)
    ### so will ignore for now?
    for question_num in range(1,5):
        if question_num == 4: # This is Proton beam specific.
            # @TODO something else?
            pass 
        else:
            # get worker quality estimates, which we'll use to 
            # scale the rationales
            worker_qualities = estimate_quality_for_q(annotations, question_num, pmids=train_pmids)
            
            # average these? 
            for w, w_q in worker_qualities.items():
                overall_worker_quality_d[w].append(w_q)

            # these are now dictionaries mapping rationales to 
            # lists of workers that provided them
            pos_rationales_d_q, neg_rationales_d_q = get_q_rationales(annotations, 
                                                            question_num, pmids=train_pmids)

            pos_rationales_d.update(pos_rationales_d_q)
            neg_rationales_d.update(neg_rationales_d_q)

            
    average_worker_qualities = {}
    # take an average for workers
    for w, qualities in overall_worker_quality_d.items(): 
        average_worker_qualities[w] = np.average(qualities)


    # collapse to unique list
    pos_rationale_worker_ids, unique_pos_rationales = get_unique(pos_rationales_d, average_worker_qualities)
    neg_rationale_worker_ids, unique_neg_rationales = get_unique(neg_rationales_d, average_worker_qualities)

    # note that this technically gives us tfidf vectors, but we only use 
    # these to look up non-zero entries anyway (otherwise tf-idf would be a
    # little weird here)
    X_pos_rationales = vectorizer.transform(unique_pos_rationales)
    X_neg_rationales = vectorizer.transform(unique_neg_rationales)

    # ok, build the model already
    # hyper-params first (for gridsearch)
    alpha_vals = 10.0**-np.arange(1,7)
    C_vals = 10.0**-np.arange(0,4)
    C_contrast_vals = 10.0**-np.arange(1,4)
    mu_vals = 10.0**np.arange(1,4)

    params_d = {"alpha": alpha_vals, 
                "C":C_vals, 
                "C_contrast_scalar":C_contrast_vals,
                "mu":mu_vals}        


    # note that you pass in the training data here, which is a little
    # weird and deviates from the usual sklearn way of doing things,
    # because this makes generating and keeping the rationales around
    # much more efficient
    if not use_worker_qualities:
        worker_qualities = None

    model = ar.ARModel(X_pos_rationales, X_neg_rationales,
                         pos_rationale_worker_ids, neg_rationale_worker_ids,
                         worker_qualities,
                         loss="log",
                         n_jobs=n_jobs)
    print "cv fitting!!"
    X_train = X[train_indices]
    model.cv_fit(X_train, train_y, alpha_vals, C_vals, C_contrast_vals, mu_vals)

    return model 



def get_q_models(annotations, X, pmids, train_pmids, vectorizer, 
                    model="cf-stacked", use_worker_qualities=True, use_rationales=False, n_jobs=1):
    q_models = []
    
    # note that we skip the last (4th) question because it
    # is sample size!
    for question_num in range(1,4):    
        # get worker quality estimates, which we'll use to 
        # scale the rationales
        worker_qualities = estimate_quality_for_q(annotations, 
            question_num, pmids=train_pmids)

        # recall that pmids aligns with X. 
        train_indicators = np.in1d(pmids, train_pmids)
        X_train = X[train_indicators]
        

        q_lbls, q_X_train, q_X_train_indices = [], [], []

        worker_ids = []
        # build up a labels vector for this question, just using
        # majority vote.
        for i, pmid in enumerate(train_pmids):
            cur_pmid_annotations = annotations[annotations['documentId'] == pmid]

            q_decisions_for_pmid = list(cur_pmid_annotations['q%s' % question_num].values)
            cur_workers = list(cur_pmid_annotations['workerId'].values)


            #q_decisions_for_pmid = \
            #    list(annotations[annotations['documentId'] == pmid]['q%s' % question_num].values)
            
            absent_votes = q_decisions_for_pmid.count("\\N") + q_decisions_for_pmid.count("")

            if absent_votes == len(q_decisions_for_pmid):
                pass 
            else: 
                #q_X_train.append(X_train[i])
                for decision_index, d in enumerate(q_decisions_for_pmid):
                    if d == "\\N":
                        pass 
                    else:
                        worker_ids.append(cur_workers[decision_index])
                        q_X_train_indices.append(i)
                        if d in ("No", "no"):
                            q_lbls.append(-1)
                        else:
                            q_lbls.append(1)

                

                '''
                number_of_labels = float(len(q_decisions_for_pmid) - absent_votes)
                no_votes = q_decisions_for_pmid.count("No") # all other decisions we take as yes
                if no_votes == number_of_labels:
                    #if no_votes > number_of_labels / 2.0:
                    q_lbls.append(-1)
                else:
                    q_lbls.append(1)
                '''
        q_X_train = X_train[q_X_train_indices]

        if(use_rationales):
            # TODO(byron.wallace@utexas.edu): Consider moving, or at least extending, this block
            # Specifically we have several methods which call this method, but they don't necessarily all want the
            # rationales to be incorporated on a per question basis.

            # annotator rationale model
            ##
            # now load in and encode the rationales
            #pos_rationales, pos_rationale_worker_ids, \
            #    neg_rationales, neg_rationale_worker_ids 

            # these are now dictionaries mapping rationales to 
            # lists of workers that provided them
            pos_rationales_d, neg_rationales_d = get_q_rationales(annotations, 
                                                            question_num, pmids=train_pmids)

            # collapse to unique list
            pos_rationale_worker_ids, unique_pos_rationales = get_unique(pos_rationales_d, worker_qualities)
            neg_rationale_worker_ids, unique_neg_rationales = get_unique(neg_rationales_d, worker_qualities)



            # note that this technically gives us tfidf vectors, but we only use 
            # these to look up non-zero entries anyway (otherwise tf-idf would be a
            # little weird here)
            X_pos_rationales = vectorizer.transform(unique_pos_rationales)
            X_neg_rationales = vectorizer.transform(unique_neg_rationales)

            
            # ok, build the model already
            # hyper-params first (for gridsearch)
            alpha_vals = 10.0**-np.arange(1,7)
            C_vals = 10.0**-np.arange(0,4)
            C_contrast_vals = 10.0**-np.arange(1,4)
            mu_vals = 10.0**np.arange(1,4)

            params_d = {"alpha": alpha_vals, 
                        "C":C_vals, 
                        "C_contrast_scalar":C_contrast_vals,
                        "mu":mu_vals}        


            # note that you pass in the training data here, which is a little
            # weird and deviates from the usual sklearn way of doing things,
            # because this makes generating and keeping the rationales around
            # much more efficient
            if not use_worker_qualities:
                worker_qualities = None

            q_model = ar.ARModel(X_pos_rationales, X_neg_rationales,
                                 pos_rationale_worker_ids, neg_rationale_worker_ids,
                                 worker_qualities,
                                 loss="log",
                                 n_jobs=n_jobs)
            print "cv fitting!!"
            q_model.cv_fit(q_X_train, q_lbls, alpha_vals, C_vals, C_contrast_vals, mu_vals)
            q_models.append(q_model)
            #q_model = ar.ARModel(X_pos_rationales, X_neg_rationales, loss="log")
        else:
            #pdb.set_trace()
            params_d = {"alpha": 10.0**-np.arange(1,7)}
            q_model = SGDClassifier(class_weight=None, loss="log", random_state=42, n_jobs=n_jobs)

            import random
            weights = None 
            if use_worker_qualities:
                weights = [worker_qualities[w_id] for w_id in worker_ids]
                #weights = [0 for w_id in worker_ids]

            clf = GridSearchCV(q_model, params_d, scoring='f1', 
                                fit_params={'sample_weight':weights}, n_jobs=n_jobs)
            
            clf.fit(q_X_train, q_lbls)#sample_weight=weights)
            #best_clf = clf.estimator 
            #best_clf.fit(q_X_train, q_lbls, sample_weight=weights)

            #pdb.set_trace()
            q_models.append(clf)

    return q_models

                # annotations[annotations['documentId'].isin(train_pmids)]['q1']
'''
def process_pilot_results(annotations_path = "pilot-data/pilotresults.csv"):
    annotations = pd.read_csv("pilot-data/pilotresults.csv", delimiter="|", header=None)
    annotations.columns = HEADERS

    # for each question, assemble separate labels/rationales file
    for q in range(1,5):
        with open("qlabels")
'''
