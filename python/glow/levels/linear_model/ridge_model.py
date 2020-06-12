from .ridge_udfs import *
from nptyping import Float, NDArray
import pandas as pd
from pyspark.sql import DataFrame
from pyspark.sql.functions import pandas_udf, PandasUDFType
import pyspark.sql.functions as f
from pyspark.sql.window import Window
from typeguard import typechecked
from typing import Any, Dict, List


@typechecked
class RidgeReducer:
    """
    The RidgeReducer class is intended to reduce the feature space of an N by M block matrix X to an N by P<<M block
    matrix.  This is done by fitting K ridge models within each block of X on one or more target labels, such that a
    block with L columns to begin with will be reduced to a block with K columns, where each column is the prediction
    of one ridge model for one target label.
    """
    def __init__(self, alphas: NDArray[(Any, ), Float]) -> None:
        """
        RidgeReducer is initialized with a list of alpha values.

        Args:
            alphas : array_like of alpha values used in the ridge reduction
        """
        if not (alphas >= 0).all():
            raise Exception('Alpha values must all be non-negative.')
        self.alphas = {f'alpha_{i}': a for i, a in enumerate(alphas)}

    def fit(
        self,
        blockdf: DataFrame,
        labeldf: pd.DataFrame,
        sample_blocks: Dict[str, List[str]],
        covdf: pd.DataFrame = pd.DataFrame({})) -> DataFrame:
        """
        Fits a ridge reducer model, represented by a Spark DataFrame containing coefficients for each of the ridge
        alpha parameters, for each block in the starting matrix, for each label in the target labels.

        Args:
            blockdf : Spark DataFrame representing the beginning block matrix X
            labeldf : Pandas DataFrame containing the target labels used in fitting the ridge models
            sample_blocks : Dict containing a mapping of sample_block ID to a list of corresponding sample IDs
            covdf : Pandas DataFrame containing covariates to be included in every model in the stacking
                ensemble (optional).

        Returns:
            Spark DataFrame containing the model resulting from the fitting routine.
        """

        map_key_pattern = ['header_block', 'sample_block']
        reduce_key_pattern = ['header_block', 'header']

        if 'label' in blockdf.columns:
            map_key_pattern.append('label')
            reduce_key_pattern.append('label')

        map_udf = pandas_udf(
            lambda key, pdf: map_normal_eqn(key, map_key_pattern, pdf, labeldf, sample_blocks, covdf
                                            ), normal_eqn_struct, PandasUDFType.GROUPED_MAP)
        reduce_udf = pandas_udf(lambda key, pdf: reduce_normal_eqn(key, reduce_key_pattern, pdf),
                                normal_eqn_struct, PandasUDFType.GROUPED_MAP)
        model_udf = pandas_udf(
            lambda key, pdf: solve_normal_eqn(key, map_key_pattern, pdf, labeldf, self.alphas, covdf
                                              ), model_struct, PandasUDFType.GROUPED_MAP)

        return blockdf \
            .groupBy(map_key_pattern) \
            .apply(map_udf) \
            .groupBy(reduce_key_pattern) \
            .apply(reduce_udf) \
            .groupBy(map_key_pattern) \
            .apply(model_udf)

    def transform(self,
                  blockdf: DataFrame,
                  labeldf: pd.DataFrame,
                  sample_blocks: Dict[str, List[str]],
                  modeldf: DataFrame,
                  covdf: pd.DataFrame = pd.DataFrame({})) -> DataFrame:
        """
        Transforms a starting block matrix to the reduced block matrix, using a reducer model produced by the
        RidgeReducer fit method.

        Args:
            blockdf : Spark DataFrame representing the beginning block matrix
            labeldf : Pandas DataFrame containing the target labels used in fitting the ridge models
            sample_blocks: Dict containing a mapping of sample_block ID to a list of corresponding sample IDs
            modeldf : Spark DataFrame produced by the RidgeReducer fit method, representing the reducer model
            covdf : Pandas DataFrame containing covariates to be included in every model in the stacking
                ensemble (optional).

        Returns:
             Spark DataFrame representing the reduced block matrix
        """

        transform_key_pattern = ['header_block', 'sample_block']

        if 'label' in blockdf.columns:
            transform_key_pattern.append('label')
            joined = blockdf.drop('sort_key').join(modeldf, ['header_block', 'sample_block', 'header'], 'right') \
                .withColumn('label', f.coalesce(f.col('label'), f.col('labels').getItem(0)))
        else:
            joined = blockdf.drop('sort_key').join(
                modeldf, ['header_block', 'sample_block', 'header'], 'right')

        transform_udf = pandas_udf(
            lambda key, pdf: apply_model(key, transform_key_pattern, pdf, labeldf, sample_blocks,
                                         self.alphas, covdf), reduced_matrix_struct,
            PandasUDFType.GROUPED_MAP)

        return joined \
            .groupBy(transform_key_pattern) \
            .apply(transform_udf)


@typechecked
class RidgeRegression:
    """
    The RidgeRegression class is used to fit ridge models against one or labels optimized over a provided list of
    ridge alpha parameters.  It is similar in function to RidgeReducer except that whereas RidgeReducer attempts to
    reduce a starting matrix X to a block matrix of smaller dimension, RidgeRegression is intended to find an optimal
    model of the form Y_hat ~ XB, where Y_hat is a matrix of one or more predicted labels and B is a matrix of
    coefficients.  The optimal ridge alpha value is chosen for each label by maximizing the average out of fold r2
    score.
    """
    def __init__(self, alphas: NDArray[(Any, ), Float]) -> None:
        """
        RidgeRegression is initialized with a list of alpha values.

        Args:
            alphas : array_like of alpha values used in the ridge regression
        """
        if not (alphas >= 0).all():
            raise Exception('Alpha values must all be non-negative.')
        self.alphas = {f'alpha_{i}': a for i, a in enumerate(alphas)}

    def fit(
        self,
        blockdf: DataFrame,
        labeldf: pd.DataFrame,
        sample_blocks: Dict[str, List[str]],
        covdf: pd.DataFrame = pd.DataFrame({})
    ) -> (DataFrame, DataFrame):
        """
        Fits a ridge regression model, represented by a Spark DataFrame containing coefficients for each of the ridge
        alpha parameters, for each block in the starting matrix, for each label in the target labels, as well as a
        Spark DataFrame containing the optimal ridge alpha value for each label.

        Args:
            blockdf : Spark DataFrame representing the beginning block matrix X
            labeldf : Pandas DataFrame containing the target labels used in fitting the ridge models
            sample_blocks : Dict containing a mapping of sample_block ID to a list of corresponding sample IDs
            covdf : Pandas DataFrame containing covariates to be included in every model in the stacking
                ensemble (optional).

        Returns:
            Two Spark DataFrames, one containing the model resulting from the fitting routine and one containing the
            results of the cross validation procedure.
        """

        map_key_pattern = ['sample_block', 'label']
        reduce_key_pattern = ['header_block', 'header', 'label']

        map_udf = pandas_udf(
            lambda key, pdf: map_normal_eqn(key, map_key_pattern, pdf, labeldf, sample_blocks, covdf
                                            ), normal_eqn_struct, PandasUDFType.GROUPED_MAP)
        reduce_udf = pandas_udf(lambda key, pdf: reduce_normal_eqn(key, reduce_key_pattern, pdf),
                                normal_eqn_struct, PandasUDFType.GROUPED_MAP)
        model_udf = pandas_udf(
            lambda key, pdf: solve_normal_eqn(key, map_key_pattern, pdf, labeldf, self.alphas, covdf
                                              ), model_struct, PandasUDFType.GROUPED_MAP)
        score_udf = pandas_udf(
            lambda key, pdf: score_models(key, map_key_pattern, pdf, labeldf, sample_blocks, self.
                                          alphas, covdf), cv_struct, PandasUDFType.GROUPED_MAP)

        modeldf = blockdf \
            .groupBy(map_key_pattern) \
            .apply(map_udf) \
            .groupBy(reduce_key_pattern) \
            .apply(reduce_udf) \
            .groupBy(map_key_pattern) \
            .apply(model_udf)

        cvdf = blockdf.drop('header_block', 'sort_key') \
            .join(modeldf, ['header', 'sample_block'], 'right') \
            .withColumn('label', f.coalesce(f.col('label'), f.col('labels').getItem(0)))\
            .groupBy(map_key_pattern) \
            .apply(score_udf) \
            .groupBy('label', 'alpha').agg(f.mean('r2').alias('r2_mean')) \
            .withColumn('modelRank', f.dense_rank().over(Window.partitionBy("label").orderBy(f.desc("r2_mean")))) \
            .filter(f'modelRank = 1') \
            .drop('modelRank')

        return modeldf, cvdf

    def transform(self,
                  blockdf: DataFrame,
                  labeldf: pd.DataFrame,
                  sample_blocks: Dict[str, List[str]],
                  modeldf: DataFrame,
                  cvdf: DataFrame,
                  covdf: pd.DataFrame = pd.DataFrame({})) -> DataFrame:
        """
        Generates predictions for the target labels in the provided label DataFrame by applying the model resulting from
        the RidgeRegression fit method to the starting block matrix.

        Args:
            blockdf : Spark DataFrame representing the beginning block matrix X
            labeldf : Pandas DataFrame containing the target labels used in fitting the ridge models
            sample_blocks: Dict containing a mapping of sample_block ID to a list of corresponding sample IDs
            modeldf : Spark DataFrame produced by the RidgeRegression fit method, representing the reducer model
            cvdf : Spark DataFrame produced by the RidgeRegression fit method, containing the results of the cross
            validation routine.
            covdf : Pandas DataFrame containing covariates to be included in every model in the stacking
                ensemble (optional).

        Returns:
            Spark DataFrame containing prediction y_hat values for each sample_block of samples for each label
        """

        transform_key_pattern = ['sample_block', 'label']

        transform_udf = pandas_udf(
            lambda key, pdf: apply_model(key, transform_key_pattern, pdf, labeldf, sample_blocks,
                                         self.alphas, covdf), reduced_matrix_struct,
            PandasUDFType.GROUPED_MAP)

        return blockdf.drop('header_block', 'sort_key').join(modeldf.drop('header_block'), ['sample_block', 'header'], 'right') \
            .withColumn('label', f.coalesce(f.col('label'), f.col('labels').getItem(0))) \
            .groupBy(transform_key_pattern) \
            .apply(transform_udf) \
            .join(cvdf, ['label', 'alpha'], 'inner') \
            .select('sample_block', 'label', 'alpha', 'values')