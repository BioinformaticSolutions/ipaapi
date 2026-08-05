"""Python client for QIAGEN Ingenuity Pathway Analysis (IPA).

Upload a dataset into an IPA project using an explicit column mapping, submit it
for analysis, and track or retrieve the results.

Typical use::

    from ipaapi import (
        ColumnMapping, Dataset, IPAClient, Measurement, MeasurementType, Observation,
    )

    mapping = ColumnMapping(
        gene_id_column="ID",
        gene_id_type="ensembl",
        observations=[
            Observation("Gemfib vs ctrl", [
                Measurement("Fold Change", MeasurementType.FOLD_CHANGE, cutoff=1.5),
                Measurement("p-value", MeasurementType.P_VALUE),
                Measurement("FDR", MeasurementType.FALSE_DISCOVERY, cutoff=0.01),
            ]),
        ],
    )

    dataset = Dataset.from_file("Data/Gemfibrozil vs Ctrl RNAseq.txt", mapping)
    client = IPAClient.login()
    analysis_ids = client.submit(dataset, project="PythonAPI_Demo")
    client.wait_for(analysis_ids)

This package builds on QIAGEN's ``python-api-demo`` example code. It is not an
official QIAGEN product.
"""

from __future__ import annotations

from .auth import (
    AUTHORIZATION_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_HOST,
    TOKEN_URL,
    Credentials,
    TokenCache,
    login,
)
from .client import AnalysisResults, IPAClient
from .dataset import Dataset, load_table
from .errors import (
    AnalysisError,
    AuthenticationError,
    IPAError,
    MappingError,
    ResultsUnavailableError,
    SubmissionError,
)
from .mapping import ColumnMapping, Measurement, Observation
from .models import AnalysisStatus, MeasurementType, ReferenceSet

#: Single source of truth for the package version; pyproject.toml reads it from
#: here at build time. Bump it in this file and nowhere else.
__version__ = "0.2.2"

__all__ = [
    "__version__",
    # mapping and data
    "ColumnMapping",
    "Measurement",
    "Observation",
    "Dataset",
    "load_table",
    # enums
    "MeasurementType",
    "AnalysisStatus",
    "ReferenceSet",
    # auth
    "login",
    "Credentials",
    "TokenCache",
    "DEFAULT_CLIENT_ID",
    "DEFAULT_HOST",
    "AUTHORIZATION_BASE_URL",
    "TOKEN_URL",
    # client
    "IPAClient",
    "AnalysisResults",
    # errors
    "IPAError",
    "AuthenticationError",
    "MappingError",
    "SubmissionError",
    "AnalysisError",
    "ResultsUnavailableError",
]
