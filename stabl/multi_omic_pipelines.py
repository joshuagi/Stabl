import pandas as pd
import numpy as np
import os
from pathlib import Path
from sklearn import clone

from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, LogisticRegressionCV, LinearRegression, LassoCV
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.feature_selection import VarianceThreshold
from sklearn.model_selection import RepeatedKFold, RepeatedStratifiedKFold, LeaveOneOut
from sklearn.svm import l1_min_c

from sklearn.utils.validation import check_is_fitted
from sklearn.linear_model import Lasso
from sklearn.model_selection import GroupShuffleSplit
from sklearn.metrics import roc_auc_score, average_precision_score, r2_score, mean_squared_error, mean_absolute_error
from scipy.stats import mannwhitneyu

from .stabl import Stabl, plot_stabl_path, plot_fdr_graph, save_stabl_results
from .pipelines_utils import compute_scores_table, save_plots, compute_scores_table_multiomic
from .preprocessing import remove_low_info_samples, LowInfoFilter
from .utils import compute_CI, permutation_test_between_clfs
from .metrics import jaccard_matrix

from .stacked_generalization import stacked_multi_omic
import random


lasso_cv = LassoCV(n_alphas=50, max_iter=int(1e6), n_jobs=-1)
logit_lasso_cv = LogisticRegressionCV(penalty="l1", solver="liblinear", Cs=np.logspace(-2, 2, 50),
                                      max_iter=int(1e6), class_weight="balanced", scoring="roc_auc",
                                      n_jobs=-1
                                      )

logit = LogisticRegression(penalty=None, class_weight="balanced", max_iter=int(1e6))
linreg = LinearRegression()

preprocessing = Pipeline(
    steps=[
        ("variance", VarianceThreshold(0.0)),
        ("lif", LowInfoFilter()),
        ("impute", SimpleImputer(strategy="median")),
        ("std", StandardScaler())
    ]
)


def multi_omic_stabl_cv(
        data_dict,
        y,
        outer_splitter,
        stabl,
        stability_selection,
        task_type,
        save_path,
        outer_groups=None
):
    """

    Parameters
    ----------
    stability_selection: Stabl
    data_dict: dict
        Dictionary containing the input omic-files.

    y: pd.Series
        pandas Series containing the outcomes for the use case. Note that y should contains the union of outcomes for
        the data_dict.

    outer_splitter: sklearn.model_selection._split.BaseCrossValidator
        Outer cross validation splitter

    stabl: SurgeLibrary.stability_selection.StabilitySelection
        STABL used to select features at each fold of the cross validation and for each omic.

    task_type: str
        Can either be "binary" for binary classification or "regression" for regression tasks.

    save_path: Path or str
        Where to save the results

    outer_groups: pd.Series, default=None
        If used, should be the same size as y and should indicate the groups of the samples.

    Returns
    -------

    """
    models = ["STABL", "SS 03", "SS 05", "SS 08", "EF Lasso"]

    os.makedirs(Path(save_path, "Training CV"), exist_ok=True)
    os.makedirs(Path(save_path, "Summary"), exist_ok=True)

    # Initializing the df containing the data of all omics
    X_tot = pd.concat(data_dict.values(), axis="columns")
    predictions_dict = dict()
    selected_features_dict = dict()

    for model in models:
        predictions_dict[model] = pd.DataFrame(data=None, index=y.index)
        selected_features_dict[model] = []

    i = 1
    for train, test in outer_splitter.split(X_tot, y, groups=outer_groups):
        print(f" Iteration {i} over {outer_splitter.get_n_splits()} ".center(80, '*'), "\n")
        train_idx, test_idx = y.iloc[train].index, y.iloc[test].index

        fold_selected_features = dict()
        for model in models:
            fold_selected_features[model] = []

        print(f"{len(train_idx)} train samples, {len(test_idx)} test samples")

        for omic_name, X_omic in data_dict.items():
            X_tmp: pd.DataFrame = X_omic.drop(index=test_idx, errors="ignore")

            # Preprocessing of X_tmp
            X_tmp = remove_low_info_samples(X_tmp)
            y_tmp = y.loc[X_tmp.index]

            X_tmp_std = pd.DataFrame(
                data=preprocessing.fit_transform(X_tmp),
                index=X_tmp.index,
                columns=preprocessing.get_feature_names_out()
            )

            # __STABL__
            if task_type == "binary":
                min_C = l1_min_c(X_tmp_std, y_tmp)
                lambda_grid = np.linspace(min_C, min_C * 100, 10)
                stabl.set_params(lambda_grid=lambda_grid)
                stability_selection.set_params(lambda_grid=lambda_grid)

            stabl.fit(X_tmp_std, y_tmp)
            tmp_sel_features = list(stabl.get_feature_names_out())
            fold_selected_features["STABL"].extend(tmp_sel_features)

            print(
                f"STABL finished on {omic_name} ({X_tmp.shape[0]} samples);"
                f" {len(tmp_sel_features)} features selected\n"
            )

            # __SS__
            stability_selection.fit(X_tmp_std, y_tmp)
            fold_selected_features["SS 03"] += list(stability_selection.get_feature_names_out(new_hard_threshold=.3))
            fold_selected_features["SS 05"] += list(stability_selection.get_feature_names_out(new_hard_threshold=.5))
            fold_selected_features["SS 08"] += list(stability_selection.get_feature_names_out(new_hard_threshold=.8))

        selected_features_dict["STABL"].append(fold_selected_features["STABL"])
        selected_features_dict[f"SS 03"].append(fold_selected_features["SS 03"])
        selected_features_dict[f"SS 05"].append(fold_selected_features["SS 05"])
        selected_features_dict[f"SS 08"].append(fold_selected_features["SS 08"])

        print("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        print(f"This fold: {len(fold_selected_features['STABL'])} features selected for STABL")
        print(f"This fold: {len(fold_selected_features['SS 03'])} features selected for SS 03")
        print(f"This fold: {len(fold_selected_features['SS 05'])} features selected for SS 05")
        print(f"This fold: {len(fold_selected_features['SS 08'])} features selected for SS 08")
        print("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\n")

        for model in ["STABL", "SS 03", "SS 05", "SS 08"]:
            X_train = X_tot.loc[train_idx, fold_selected_features[model]]
            X_test = X_tot.loc[test_idx, fold_selected_features[model]]
            y_train, y_test = y.loc[train_idx], y.loc[test_idx]

            if len(fold_selected_features[model]) > 0:
                # Standardization
                std_pipe = Pipeline(
                    steps=[
                        ('imputer', SimpleImputer(strategy="median")),
                        ('std', StandardScaler())
                    ]
                )

                X_train = pd.DataFrame(
                    data=std_pipe.fit_transform(X_train),
                    index=X_train.index,
                    columns=X_train.columns
                )
                X_test = pd.DataFrame(
                    data=std_pipe.transform(X_test),
                    index=X_test.index,
                    columns=X_test.columns
                )

                # __Final Models__
                if task_type == "binary":
                    predictions = clone(logit).fit(X_train, y_train).predict_proba(X_test)[:, 1].flatten()

                elif task_type == "regression":
                    predictions = clone(linreg).fit(X_train, y_train).predict(X_test)

                else:
                    raise ValueError("task_type not recognized.")

                predictions_dict[model].loc[test_idx, f'Fold n°{i}'] = predictions

            else:
                if task_type == "binary":
                    predictions_dict[model].loc[test_idx, f'Fold n°{i}'] = [0.5] * len(test_idx)

                elif task_type == "regression":
                    predictions_dict[model].loc[test_idx, f'Fold n°{i}'] = [np.mean(y_train)] * len(test_idx)

                else:
                    raise ValueError("task_type not recognized.")
        # __EF Lasso__
        X_train = X_tot.loc[train_idx]
        X_test = X_tot.loc[test_idx]
        X_train = pd.DataFrame(
            data=preprocessing.fit_transform(X_train),
            columns=preprocessing.get_feature_names_out(),
            index=X_train.index
        )

        X_test = pd.DataFrame(
            data=preprocessing.transform(X_test),
            columns=preprocessing.get_feature_names_out(),
            index=X_test.index
        )

        if task_type == "binary":
            inner_splitter = RepeatedStratifiedKFold(n_splits=5, n_repeats=5, random_state=42)
            model = clone(logit_lasso_cv).set_params(cv=inner_splitter)
            predictions = model.fit(X_train, y_train).predict_proba(X_test)[:, 1]
        else:
            inner_splitter = RepeatedKFold(n_splits=5, n_repeats=5, random_state=42)
            model = clone(lasso_cv).set_params(cv=inner_splitter)
            predictions = model.fit(X_train, y_train).predict(X_test)
            
        selected_features_dict["EF Lasso"].append(list(X_train.columns[np.where(model.coef_.flatten())]))
        predictions_dict["EF Lasso"].loc[test_idx, f"Fold n°{i}"] = predictions

        i += 1

    # __SAVING_RESULTS__

    if y.name is None:
        y.name = "outcome"

    summary_res_path = Path(save_path, "Summary")
    cv_res_path = Path(save_path, "Training CV")

    jaccard_matrix_dict = dict()
    formatted_features_dict = dict()

    for model in models:

        jaccard_matrix_dict[model] = jaccard_matrix(selected_features_dict[model])

        formatted_features_dict[model] = pd.DataFrame(
            data={
                "Fold selected features": selected_features_dict[model],
                "Fold nb of features": [len(el) for el in selected_features_dict[model]]
            },
            index=[f"Fold {i}" for i in range(outer_splitter.get_n_splits())]
        )
        formatted_features_dict[model].to_csv(Path(cv_res_path, f"Selected Features {model}.csv"))

    predictions_dict = {model: predictions_dict[model].median(axis=1) for model in predictions_dict.keys()}

    table_of_scores = compute_scores_table(
        predictions_dict=predictions_dict,
        y=y,
        task_type=task_type,
        selected_features_dict=formatted_features_dict
    )

    table_of_scores.to_csv(Path(summary_res_path, "Scores training CV.csv"))
    table_of_scores.to_csv(Path(cv_res_path, "Scores training CV.csv"))

    save_plots(
        predictions_dict=predictions_dict,
        y=y,
        task_type=task_type,
        save_path=cv_res_path
    )

    return predictions_dict


def multi_omic_stabl(
        data_dict,
        y,
        stabl,
        stability_selection,
        task_type,
        save_path,
        X_test=None,
        y_test=None
):
    """

    Parameters
    ----------
    stability_selection
    data_dict

    y

    stabl: SurgeLibrary.stability_selection.StabilitySelection

    task_type

    save_path

    X_test: pd.DataFrame, default=None

    y_test: pd.Series, default=None

    Returns
    -------

    """
    models = ["STABL", "SS 03", "SS 05", "SS 08", "EF Lasso"]

    os.makedirs(Path(save_path, "Training-Validation"), exist_ok=True)
    os.makedirs(Path(save_path, "Summary"), exist_ok=True)

    # Initializing the df containing the data of all omics
    X_tot = pd.concat(data_dict.values(), axis="columns")

    predictions_dict = dict()
    selected_features_dict = dict()
    for model in models:
        selected_features_dict[model] = []

    for omic_name, X_omic in data_dict.items():
        X_omic_std = pd.DataFrame(
            data=preprocessing.fit_transform(X_omic),
            index=X_omic.index,
            columns=preprocessing.get_feature_names_out()
        )
        y_omic = y.loc[X_omic_std.index]

        stabl.fit(X_omic_std, y_omic)
        omic_selected_features = stabl.get_feature_names_out()
        selected_features_dict["STABL"] += list(omic_selected_features)

        print(f"STABL finished on {omic_name}; {len(omic_selected_features)} features selected")

        save_stabl_results(
            stabl=stabl,
            path=Path(save_path, "Training-Validation", f"STABL results on {omic_name}"),
            df_X=X_omic,
            y=y_omic,
            task_type=task_type
        )

        stability_selection.fit(X_omic_std, y_omic)
        selected_features_dict["SS 03"] += list(stability_selection.get_feature_names_out(new_hard_threshold=.3))
        save_stabl_results(stabl=stability_selection,
                           path=Path(save_path, "Training-Validation", f"SS 03 results on {omic_name}"), df_X=X_omic,
                           y=y_omic, task_type=task_type, new_hard_threshold=.3)
        selected_features_dict["SS 05"] += list(stability_selection.get_feature_names_out(new_hard_threshold=.5))
        save_stabl_results(stabl=stability_selection,
                           path=Path(save_path, "Training-Validation", f"SS 05 results on {omic_name}"), df_X=X_omic,
                           y=y_omic, task_type=task_type, new_hard_threshold=.5)
        selected_features_dict["SS 08"] += list(stability_selection.get_feature_names_out(new_hard_threshold=.8))
        save_stabl_results(stabl=stability_selection,
                           path=Path(save_path, "Training-Validation", f"SS 08 results on {omic_name}"), df_X=X_omic,
                           y=y_omic, task_type=task_type, new_hard_threshold=.8)

    final_prepro = Pipeline(
        steps=[("impute", SimpleImputer(strategy="median")), ("std", StandardScaler())]
    )

    for model in ["STABL", "SS 03", "SS 05", "SS 08"]:
        X_train = X_tot[selected_features_dict[model]]
        X_train_std = pd.DataFrame(
            data=final_prepro.fit_transform(X_train),
            index=X_tot.index,
            columns=final_prepro.get_feature_names_out()
        )

        if task_type == "binary":
            base_linear_model = logit

        else:
            base_linear_model = linreg

        base_linear_model.fit(X_train_std, y)
        base_linear_model_coef = pd.DataFrame(
            {"Feature": selected_features_dict[model],
             "Associated weight": base_linear_model.coef_.flatten()
             }
        ).set_index("Feature")
        base_linear_model_coef.to_csv(Path(save_path, "Training-Validation", f"{model} coefficients.csv"))

        if X_test is not None:
            X_test_std = pd.DataFrame(
                data=final_prepro.transform(X_test[selected_features_dict[model]]),
                index=X_test.index,
                columns=final_prepro.get_feature_names_out()
            )
            if task_type == "binary":
                model_preds = base_linear_model.predict_proba(X_test_std)[:, 1]
            else:
                model_preds = base_linear_model.predict(X_test_std)

            predictions_dict[model] = pd.Series(
                model_preds,
                index=y_test.index,
                name=f"{model} predictions"
            )

    # __EF Lasso__
    X_train_std = pd.DataFrame(
        data=final_prepro.fit_transform(X_tot),
        index=X_tot.index,
        columns=final_prepro.get_feature_names_out()
    )

    if task_type == "binary":
        inner_splitter = RepeatedStratifiedKFold(n_splits=5, n_repeats=5, random_state=42)
        model = clone(logit_lasso_cv).set_params(cv=inner_splitter).fit(X_train_std, y)
    else:
        inner_splitter = RepeatedKFold(n_splits=5, n_repeats=5, random_state=42)
        model = clone(lasso_cv).set_params(cv=inner_splitter).fit(X_train_std, y)

    selected_features_dict["EF Lasso"] += list(X_train_std.columns[np.where(model.coef_.flatten())])

    lasso_coef = pd.DataFrame(
        {"Feature": selected_features_dict["EF Lasso"],
         "Associated weight": model.coef_.flatten()[np.where(model.coef_.flatten())]
         }
    ).set_index("Feature")
    lasso_coef.to_csv(Path(save_path, "Training-Validation", f"EF Lasso coefficients.csv"))

    if X_test is not None:
        X_test_std = pd.DataFrame(
            data=final_prepro.transform(X_test),
            index=X_test.index,
            columns=final_prepro.get_feature_names_out()
        )

        predictions_dict["EF Lasso"] = pd.Series(
            data=model.predict(X_test_std) if task_type == "regression" else model.predict_proba(X_test_std)[:, 1],
            index=y_test.index,
            name="EF Lasso predictions"
        )

        save_plots(
            predictions_dict=predictions_dict,
            y=y_test,
            task_type=task_type,
            save_path=Path(save_path, "Training-Validation"),
        )

        validation_scores = compute_scores_table(
            predictions_dict=predictions_dict,
            y=y_test,
            task_type=task_type,
        )
        validation_scores.to_csv(Path(save_path, "Training-Validation", "Scores on Validation.csv"))
        validation_scores.to_csv(Path(save_path, "Summary", "Scores on Validation.csv"))

    return predictions_dict


def late_fusion_lasso_cv(train_data_dict, y, outer_splitter, task_type, save_path, groups=None):

    predictions_dict = {model: pd.DataFrame(data=None, index=y.index) for model in train_data_dict.keys()}
    omics_selected_features = {model: [] for model in train_data_dict.keys()}

    for omic_name, X_omic in train_data_dict.items():
        y_omic = y.loc[X_omic.index]
        i = 1
        print(f"Omic {omic_name}")
        for train, test in outer_splitter.split(X_omic, y_omic, groups=groups):
            print(f"Iteration {i} over {outer_splitter.get_n_splits()}")

            train_idx, test_idx = y_omic.iloc[train].index, y_omic.iloc[test].index
            X_train, X_test = X_omic.loc[train_idx], X_omic.loc[test_idx]
            y_train, y_test = y_omic.loc[train_idx], y_omic.loc[test_idx]

            X_train_std = pd.DataFrame(
                data=preprocessing.fit_transform(X_train),
                index=X_train.index,
                columns=preprocessing.get_feature_names_out()
            )

            X_test_std = pd.DataFrame(
                data=preprocessing.transform(X_test),
                index=X_test.index,
                columns=preprocessing.get_feature_names_out()
            )

            if task_type == "binary":
                inner_splitter = RepeatedStratifiedKFold(n_splits=5, n_repeats=5, random_state=42)
                model = clone(logit_lasso_cv).set_params(cv=inner_splitter)
                predictions = model.fit(X_train_std, y_train).predict_proba(X_test_std)[:, 1]
            else:
                inner_splitter = RepeatedKFold(n_splits=5, n_repeats=5, random_state=42)
                model = clone(lasso_cv).set_params(cv=inner_splitter)
                predictions = model.fit(X_train_std, y_train).predict(X_test_std)

            predictions_dict[omic_name].loc[test_idx, f"Fold n°{i}"] = predictions

            omics_selected_features[omic_name].append(list(X_train_std.columns[np.where(model.coef_.flatten())]))
            i += 1

    all_selected_features = []
    for j in range(outer_splitter.get_n_splits()):
        fold_selected_features = []
        for model in omics_selected_features.keys():
            fold_selected_features += omics_selected_features[model][j]
        all_selected_features.append(fold_selected_features)

    for model, predictions in predictions_dict.items():
        predictions_dict[model] = predictions.median(1)

    df_predictions = pd.DataFrame(pd.concat(predictions_dict.values(), axis=1))
    df_predictions.columns = list(predictions_dict.keys())

    stacked_df, weights = stacked_multi_omic(df_predictions, y, task_type)
    saving_path = Path(save_path, "Training CV", "LF Lasso")
    os.makedirs(saving_path, exist_ok=True)

    all_selected_features = pd.DataFrame(
        data={
            "Fold selected features": all_selected_features,
            "Fold nb of features": [len(el) for el in all_selected_features]
        },
        index=[f"Fold {i}" for i in range(outer_splitter.get_n_splits())]
    )

    all_selected_features.to_csv(Path(save_path, "Training CV", "Selected Features LF Lasso.csv"))

    weights.to_csv(Path(saving_path, "Associated weights.csv"))
    stacked_df.to_csv(Path(saving_path, "Stacked Generalization predictions.csv"))
    
    predictions_dict = {"LF Lasso": stacked_df["Stacked Gen. Predictions"]}
    save_plots(predictions_dict, y, task_type=task_type, save_path=Path(save_path, "Training CV"))
    

def multi_omic_stabl_cv_josh(
        data_dict,
        y,
        outer_splitter,
        stabl,
        stability_selection,
        task_type,
        save_path,
        outer_groups=None
):
    """

    Parameters
    ----------
    stability_selection: Stabl
    data_dict: dict
        Dictionary containing the input omic-files.

    y: pd.Series
        pandas Series containing the outcomes for the use case. Note that y should contains the union of outcomes for
        the data_dict.

    outer_splitter: sklearn.model_selection._split.BaseCrossValidator
        Outer cross validation splitter

    stabl: SurgeLibrary.stability_selection.StabilitySelection
        STABL used to select features at each fold of the cross validation and for each omic.

    task_type: str
        Can either be "binary" for binary classification or "regression" for regression tasks.

    save_path: Path or str
        Where to save the results

    outer_groups: pd.Series, default=None
        If used, should be the same size as y and should indicate the groups of the samples.

    Returns
    -------

    """
    ##################################################################
    # Set some parameters for testing the cross-validation loop
    # data_dict = train_data_dict
    # y = response
    # save_path = result_folder
    # outer_groups=None # For LeaveOneOut
    #outer_groups=patients_id # For GroupShuffleSplit outer splitter
    ##################################################################
    
    
    os.makedirs(Path(save_path, "Training CV"), exist_ok=True) # Creates directories to store mode outputs
    #os.makedirs(Path(save_path, "Summary"), exist_ok=True)
    
    
    models = ["STABL", "SS 03", "SS 05", "SS 08", "EF Lasso"] # Specifies models. EF lasso = early fusion lasso

    # Initializing the df containing the data of all omics
    X_tot = pd.concat(data_dict.values(), axis="columns") # Concatanates all columns of the different dataframes
    mask = X_tot.index # Subset the response by the indices in the combined training data
    y = y[mask]
    
    predictions_dict = dict()
    selected_features_dict = dict()

    for model in models:
        predictions_dict[model] = pd.DataFrame(data=None, index=y.index) # Initiates an empty dataframe for every model
        selected_features_dict[model] = []
        
        
    ##################################################################
    # For testing within each iteration of the CV scheme
    # iterable = outer_splitter.split(X_tot, y, groups=outer_groups)
    # iterable_element = next(iterable)
    # train = iterable_element[0]
    # test = iterable_element[1]    
    ##################################################################
    
    i = 1
    for train, test in outer_splitter.split(X_tot, y, groups=outer_groups): 
      # This is the cross-validation step that splits the data. 
      # Note that it is performed on the total concatanated data! That means that although stabl-CV is performed per dataframe (below),
      # The splits of the groups actually happens before this. What this means is that if an sample is missing entirely from a dataframe,
      # all data from selected features is imputed and used to store a prediction on that individual. 
      # In the event that the groups are unbalanced, it is possible that imputed values may be closer to the overrepresented group.
      # In that case, the model could tend to predict the overrepresented group on samples that are entirely missing from an omic layer.
      # The final model that generates predictions in each fold accounts for unbalanced groups. 
        
        if isinstance(outer_splitter, LeaveOneOut): ##** Added for LOO, 4 lines
            print(f" Iteration {i} over {X_tot.shape[0]} ".center(80, '*'), "\n") #
        else: #
            print(f" Iteration {i} over {outer_splitter.get_n_splits()} ".center(80, '*'), "\n") # Added for LOO
        train_idx, test_idx = y.iloc[train].index, y.iloc[test].index
        

        
        
        fold_selected_features = dict() # Assigns keys to the dictionary
        for model in models:
            fold_selected_features[model] = []


        print(f"{len(train_idx)} train samples, {len(test_idx)} test samples")
        
        
        
        
        ##################################################################
        #data_dict.keys() # Look at the keys
        #X_omic = data_dict['CyTOF'] # Retrieve one and assign it for testing
        ##################################################################

        for omic_name, X_omic in data_dict.items():
          # Cross-validation that happens per dataframe
          # This trains the stabl model and selects features for every CV fold
            X_tmp: pd.DataFrame = X_omic.drop(index=test_idx, errors="ignore") # The colon here indicates 'type hinting' during variable assignment

            # Preprocessing of X_tmp
            X_tmp = remove_low_info_samples(X_tmp) # Removes samples with 'too many' NAs. By default this removes samples with all NAs
            y_tmp = y.loc[X_tmp.index]

            X_tmp_std = pd.DataFrame(
                data=preprocessing.fit_transform(X_tmp),
                index=X_tmp.index,
                columns=preprocessing.get_feature_names_out()
            )


            # __STABL__
            if task_type == "binary":
                min_C = l1_min_c(X_tmp_std, y_tmp)
                lambda_grid = np.linspace(min_C, min_C * 100, 10)
                stabl.set_params(lambda_grid=lambda_grid)
                stability_selection.set_params(lambda_grid=lambda_grid)

            stabl.fit(X_tmp_std, y_tmp)
            tmp_sel_features = list(stabl.get_feature_names_out())
            fold_selected_features["STABL"].extend(tmp_sel_features)
            
            ##################################################################
            # tmp_sel_features = random.choice([["TCRgdp.T_VDAC1_median"], []]) # Randomly 'selects' a feature
            # fold_selected_features["STABL"].extend(tmp_sel_features)
            ##################################################################

            print(
                f"STABL finished on {omic_name} ({X_tmp.shape[0]} samples);"
                f" {len(tmp_sel_features)} features selected\n"
            )




            # __SS__
            stability_selection.fit(X_tmp_std, y_tmp)
            fold_selected_features["SS 03"] += list(stability_selection.get_feature_names_out(new_hard_threshold=.3)) # Collect biomarkers for each omic and CV fold
            fold_selected_features["SS 05"] += list(stability_selection.get_feature_names_out(new_hard_threshold=.5))
            fold_selected_features["SS 08"] += list(stability_selection.get_feature_names_out(new_hard_threshold=.8))
            
            ##################################################################
            # fold_selected_features["SS 03"] += random.choice([["TCRgdp.T_VDAC1_median"], []])
            # fold_selected_features["SS 05"] += random.choice([["TCRgdp.T_VDAC1_median"], []])
            # fold_selected_features["SS 08"] += random.choice([["TCRgdp.T_VDAC1_median"], []])
            ##################################################################

        selected_features_dict["STABL"].append(fold_selected_features["STABL"]) # Add biomarkers for each CV fold
        selected_features_dict[f"SS 03"].append(fold_selected_features["SS 03"])
        selected_features_dict[f"SS 05"].append(fold_selected_features["SS 05"])
        selected_features_dict[f"SS 08"].append(fold_selected_features["SS 08"])

        print("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        print(f"This fold: {len(fold_selected_features['STABL'])} features selected for STABL")
        print(f"This fold: {len(fold_selected_features['SS 03'])} features selected for SS 03")
        print(f"This fold: {len(fold_selected_features['SS 05'])} features selected for SS 05")
        print(f"This fold: {len(fold_selected_features['SS 08'])} features selected for SS 08")
        print("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\n")
        
        
        ##################################################################
        #model = "STABL"
        ##################################################################

        for model in ["STABL", "SS 03", "SS 05", "SS 08"]:
            ##################################################################
            # sample_features = X_tot.columns[:10]
            # X_train = X_tot.loc[train_idx, sample_features] # Subset features for testing!
            # X_test = X_tot.loc[test_idx, sample_features] # Subset features for testing!
            ##################################################################
          
            
            X_train = X_tot.loc[train_idx, fold_selected_features[model]] # Subsets the selected features from both the training and test data
            # 1. Note that the features are selected above while removing low info samples. But then the model is fit to all samples to evaluate CV.
            # 2. Also note that the fit_transform step occurs after imputation, obviously
            X_test = X_tot.loc[test_idx, fold_selected_features[model]] # Retains multi-omic features
            y_train, y_test = y.loc[train_idx], y.loc[test_idx] # Run until here to test functions within each iteration (it generates splits of the data)
            
            




            if len(fold_selected_features[model]) > 0:
                # Standardization
                std_pipe = Pipeline(
                    steps=[
                        ('imputer', SimpleImputer(strategy="median")),
                        ('std', StandardScaler())
                    ]
                )

                X_train = pd.DataFrame(
                    data=std_pipe.fit_transform(X_train),
                    index=X_train.index,
                    columns=X_train.columns
                )
                X_test = pd.DataFrame(
                    data=std_pipe.transform(X_test),
                    index=X_test.index,
                    columns=X_test.columns
                )

                # __Final Models__
                if task_type == "binary":
                    #predictions = clone(logit).fit(X_train, y_train).predict_proba(X_test)[:, 1].flatten() #Default parameter is class_weight='balanced',
                    # which handles imbalanced data by assigning higher weights
                    
                    ##################################################################
                    # Generate predictions with Lasso instead of normal logit
                    # Add here predictions generated by a Logistic regression lasso
                    inner_splitter = RepeatedStratifiedKFold(n_splits=5, n_repeats=5, random_state=42) # StratifiedKFold ensures that each fold of the dataset has the same proportion of observations with a given categorical label
                    model_lasso = clone(logit_lasso_cv).set_params(cv=inner_splitter)
                    predictions = model_lasso.fit(X_train, y_train).predict_proba(X_test)[:, 1].flatten()
                    # Collect features. Create new key in dataframe if it doesnt already exist
                    features_binary_lasso = list(X_train.columns[np.where(model_lasso.coef_.flatten())])
                    coefs_binary_lasso = list(model_lasso.coef_.flatten()) # coefs for each variable
                    if "Stabl_binary_lasso" not in selected_features_dict and model == "STABL": # create new dictionary for lasso features, but only for Stabl model
                      selected_features_dict["Stabl_binary_lasso"] = [] # Create new key
                      selected_features_dict["Stabl_binary_lasso"].append(features_binary_lasso)
                      selected_features_dict["Stabl_binary_lasso_coefs"] = []
                      selected_features_dict["Stabl_binary_lasso_coefs"].append(coefs_binary_lasso)
                    elif "Stabl_binary_lasso" in selected_features_dict and model == "STABL":
                      selected_features_dict["Stabl_binary_lasso"].append(features_binary_lasso)
                      selected_features_dict["Stabl_binary_lasso_coefs"].append(coefs_binary_lasso)
                    else:
                      pass
                    ##################################################################
                    


                elif task_type == "regression":
                    predictions = clone(linreg).fit(X_train, y_train).predict(X_test)

                else:
                    raise ValueError("task_type not recognized.")
                
                ##################################################################  
                #i = 2 # testing
                ##################################################################

                predictions_dict[model].loc[test_idx, f'Fold n°{i}'] = predictions



            else: # If no features are selected, predict the intercept//0.5
                if task_type == "binary":
                    predictions_dict[model].loc[test_idx, f'Fold n°{i}'] = [0.5] * len(test_idx)
                    ##################################################################
                    # In case no features are selected by Stabl, we still need add an entry to the dictionary
                    if "Stabl_binary_lasso" not in selected_features_dict and model == "STABL":
                      selected_features_dict["Stabl_binary_lasso"] = [] # Create new key
                      selected_features_dict["Stabl_binary_lasso"].append([])
                      selected_features_dict["Stabl_binary_lasso_coefs"] = []
                      selected_features_dict["Stabl_binary_lasso_coefs"].append([])
                    elif "Stabl_binary_lasso" in selected_features_dict and model == "STABL":
                      selected_features_dict["Stabl_binary_lasso"].append([])
                      selected_features_dict["Stabl_binary_lasso_coefs"].append([])
                    else:
                      pass
                    ##################################################################

                elif task_type == "regression":
                    predictions_dict[model].loc[test_idx, f'Fold n°{i}'] = [np.mean(y_train)] * len(test_idx)

                else:
                    raise ValueError("task_type not recognized.")
        
        
        # __EF Lasso__
        X_train = X_tot.loc[train_idx]
        X_test = X_tot.loc[test_idx]
        X_train = pd.DataFrame(
            data=preprocessing.fit_transform(X_train),
            columns=preprocessing.get_feature_names_out(),
            index=X_train.index
        )

        X_test = pd.DataFrame(
            data=preprocessing.transform(X_test),
            columns=preprocessing.get_feature_names_out(),
            index=X_test.index
        )

        if task_type == "binary":
            inner_splitter = RepeatedStratifiedKFold(n_splits=5, n_repeats=5, random_state=42)
            model = clone(logit_lasso_cv).set_params(cv=inner_splitter)
            predictions = model.fit(X_train, y_train).predict_proba(X_test)[:, 1]
        else:
            inner_splitter = RepeatedKFold(n_splits=5, n_repeats=5, random_state=42)
            model = clone(lasso_cv).set_params(cv=inner_splitter)
            predictions = model.fit(X_train, y_train).predict(X_test)
            
        selected_features_dict["EF Lasso"].append(list(X_train.columns[np.where(model.coef_.flatten())]))
        predictions_dict["EF Lasso"].loc[test_idx, f"Fold n°{i}"] = predictions

        i += 1

    # __SAVING_RESULTS__

    if y.name is None:
        y.name = "outcome"

    #summary_res_path = Path(save_path, "Summary")
    cv_res_path = Path(save_path, "Training CV")

    jaccard_matrix_dict = dict()
    formatted_features_dict = dict()
    
    if "Stabl_binary_lasso" in selected_features_dict: # Create new key
      models.append("Stabl_binary_lasso")
      
    else:
      models = models   
    
    #################################################################
    #model = "Stabl_binary_lasso"
    #model = "STABL"
    ##################################################################

    for model in models:

        jaccard_matrix_dict[model] = jaccard_matrix(selected_features_dict[model]) # Calculates similarity of CV folds based on the selected features
        # Selected features at each Stabl Run
        # selected_features_dict[model][1]
        
        
        # Update for LOO
        if isinstance(outer_splitter, LeaveOneOut):
            
            formatted_features_dict[model] = pd.DataFrame(
                        data={
                            "Fold selected features": selected_features_dict[model],
                            "Fold nb of features": [len(el) for el in selected_features_dict[model]]
                        },
                        ##################################################################
                        #index=[f"Fold {i}" for i in range(2)] # testing on two iterations
                        ##################################################################
                        index=[f"Fold {i}" for i in range(X_tot.shape[0])]
                    )
    
        elif not isinstance(outer_splitter, LeaveOneOut):
            formatted_features_dict[model] = pd.DataFrame(
                        data={
                            "Fold selected features": selected_features_dict[model],
                            "Fold nb of features": [len(el) for el in selected_features_dict[model]]
                        },
                        index=[f"Fold {i}" for i in range(outer_splitter.get_n_splits())]
                    )
        #End up update

        formatted_features_dict[model].to_csv(Path(cv_res_path, f"Selected Features {model}.csv"))
        
    
    
    #! Write a file containing the model coefficients if it exists
    if "Stabl_binary_lasso_coefs" in selected_features_dict:
      formatted_coef_dict = pd.DataFrame(
                        data={
                            "Fold feature coefficients": selected_features_dict["Stabl_binary_lasso_coefs"]
                        },
                        index=[f"Fold {i}" for i in range(X_tot.shape[0])]
                    )
      formatted_coef_dict.to_csv(Path(cv_res_path, "Stabl feature coefficients.csv"))
    else:
      pass
    
    # Analyze predictions
    predictions_dict = {model: predictions_dict[model].median(axis=1) for model in predictions_dict.keys()} # Generates predictions dictionary.
    # This is specifically designed for GroupShuffleSplit, such that it takes the median prediction across CV folds
    # If the CV scheme is LOO, you only get one prediction per fold. Then this essentially collapses the predictions into one column

    table_of_scores = compute_scores_table_multiomic( # This function only creates summary statistics of the predictions without directly delivering predictions
        predictions_dict=predictions_dict,
        y=y,
        task_type=task_type,
        selected_features_dict=formatted_features_dict
    )

    #table_of_scores.to_csv(Path(summary_res_path, "Scores training CV.csv")) # Prints the results twice..
    table_of_scores.to_csv(Path(cv_res_path, "Scores training CV.csv"))

    save_plots( # This delivers the predictions for each model
        predictions_dict=predictions_dict,
        y=y,
        task_type=task_type,
        save_path=cv_res_path
    )

    return predictions_dict
  
  
