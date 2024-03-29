import io
import os
import re
from datetime import date
from datetime import datetime
from datetime import timedelta

import boto3
import pandas as pd
import sqlalchemy
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table

# S3 configuration
s3_endpoint = os.getenv("S3_ENDPOINT")
access_key = os.getenv("AWS_ACCESS_KEY")
secret_key = os.getenv("AWS_SECRET_KEY")
bucket = os.getenv("S3_BUCKET")
path = os.getenv("S3_BUCKET_PREFIX")


# Redshift configuration
DB_ENGINE = os.getenv("DATABASE_ENGINE", "postgresql")
REDSHIFT_HOST = os.getenv("REDSHIFT_HOST")
REDSHIFT_PORT = os.getenv("REDSHIFT_PORT")
REDSHIFT_DB = os.getenv("REDSHIFT_DB")
REDSHIFT_SCHEMA = os.getenv("REDSHIFT_SCHEMA")
REDSHIFT_TABLE_PREFIX = os.getenv("REDSHIFT_TABLE_PREFIX", "koku")
REDSHIFT_USER = os.getenv("REDSHIFT_USER")
REDSHIFT_PASSWORD = os.getenv("REDSHIFT_PASSWORD")
REDSHIFT_DATABASE_URI = None

if REDSHIFT_HOST:
    REDSHIFT_DATABASE_URI = f"{DB_ENGINE}://{REDSHIFT_USER}:{REDSHIFT_PASSWORD}@{REDSHIFT_HOST}:{REDSHIFT_PORT}/{REDSHIFT_DB}"  # noqa


def get_s3_client():
    session = boto3.session.Session()
    s3_client = session.client(
        service_name="s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        endpoint_url=f"https://{s3_endpoint}",
        verify=False,
    )
    return s3_client


def get_s3_resource():
    s3 = boto3.resource(
        "s3",
        endpoint_url=f"https://{s3_endpoint}",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        verify=False,
    )
    return s3


# Read single parquet file from S3
def pd_read_s3_parquet(key, bucket, s3_client=None, **args):
    if s3_client is None:
        s3_client = boto3.client("s3")
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    regex = re.compile(r"\b(\w+)=(.*?)(?=\s\w+=\s*|$)")
    key_splits = key.split("/")
    partitions = {}
    for innerkeys in key_splits:
        d = dict(regex.findall(innerkeys))
        partitions.update(d)

    dataframe = pd.read_parquet(io.BytesIO(obj["Body"].read()), **args)
    for key, value in partitions.items():
        dataframe[key] = value

    return dataframe


# Read multiple parquets from a folder on S3 generated by spark
def pd_read_s3_multiple_parquets(
    filepath, bucket, partition_filter_date, verbose=False, **args
):
    s3_client = get_s3_client()
    s3 = get_s3_resource()
    partition_filter = f"/year={partition_filter_date.year}/month={partition_filter_date.month}/day={partition_filter_date.day}/"  # noqa
    if not filepath.endswith("/"):
        filepath = filepath + "/"  # Add '/' to the end

    for item in s3.Bucket(bucket).objects.filter(Prefix=filepath):
        if partition_filter in item.key:
            print(item.key)

    s3_keys = [
        item.key
        for item in s3.Bucket(bucket).objects.filter(Prefix=filepath)
        if item.key.endswith(".parquet") and (partition_filter in item.key)
    ]
    if not s3_keys:
        print("No parquet found in", bucket, filepath, partition_filter)
    elif verbose:
        print("Load parquets:")
        for p in s3_keys:
            print(p)
    dfs = [
        pd_read_s3_parquet(key, bucket=bucket, s3_client=s3_client, **args)
        for key in s3_keys
    ]
    return pd.concat(dfs, ignore_index=True)


def str_begins_or_ends_with(column_name, str_value):
    return column_name.startswith(str_value) or column_name.endswith(str_value)


def get_column_datatype(column_name):
    data_type = String(256)

    if column_name == "domain":
        data_type = String(1024)

    if (
        str_begins_or_ends_with(column_name, "count")
        or str_begins_or_ends_with(column_name, "num")
        or column_name in ["year", "month", "day"]
    ):
        data_type = Integer
    elif (
        str_begins_or_ends_with(column_name, "timestamp")
        or str_begins_or_ends_with(column_name, "date")
        or str_begins_or_ends_with(column_name, "datetime")
    ):
        data_type = DateTime

    return data_type


def create_table(engine, schema_name, name, *cols):
    meta = MetaData(schema=schema_name)
    table = Table(name, meta, *cols, schema=schema_name)
    table.create(engine, checkfirst=True)

    stmt = f"grant ALL on {schema_name}.{name} to group rsds_pnt_rw;"
    stmt += f"grant SELECT on {schema_name}.{name} to group rsds_pnt_ro;"
    stmt = "commit;"
    with engine.connect() as con:
        con.execute(stmt)


def write_metrics(engine, schema_name, metric, dataframe):
    table_prefix = REDSHIFT_TABLE_PREFIX
    table_name = f"{table_prefix}_{metric}"

    print(f"Creating table={table_name} if it doesn't exist.")
    columns = []
    for col in dataframe.dropna(axis=1).columns:
        data_type = get_column_datatype(col)
        col_obj = Column(col, data_type, nullable=True)
        columns.append(col_obj)
        if data_type == DateTime:
            dataframe[col] = pd.to_datetime(dataframe[col]).dt.tz_localize(
                None
            )

    create_table(engine, schema_name, table_name, *columns)

    print(f"Inserting data into table={table_name}.")
    with engine.connect() as con:
        print(f"Inserting {dataframe.dropna(axis=1).dropna().shape[0]} rows.")
        dataframe.dropna(axis=1).dropna().replace("", None).to_sql(
            table_name,
            con=con,
            schema=schema_name,
            if_exists="append",
            index=False,
        )


todays_date = date.today() - timedelta(days=1)
date_override = os.getenv("DATE_OVERRIDE")
if date_override:
    format = "%Y-%m-%d"
    todays_date = datetime.strptime(date_override, format)

df = pd_read_s3_multiple_parquets("metrics", bucket, todays_date)
mask = df.applymap(type) != bool
d = {True: "true", False: "false"}
df = df.where(mask, df.replace(d))
if "additional_context" in df.columns:
    df.drop("additional_context", axis=1, inplace=True)

todays_df = df.loc[
    (df["year"] == f"{todays_date.year}")
    & (df["month"] == f"{todays_date.month}")
    & (df["day"] == f"{todays_date.day}")
]
metrics = todays_df.metric.unique()


db = sqlalchemy.create_engine(REDSHIFT_DATABASE_URI)

skipped_metrics = [
    "count_filtered_users_by_account",
    "list_filtered_accounts",
    "count_filtered_users_by_domain",
    "count_errored_clusters",
    "invalid_sources",
]
for metric in metrics:
    if metric in skipped_metrics:
        continue
    metric_df = todays_df.loc[todays_df["metric"] == metric].dropna(axis=1)
    write_metrics(db, REDSHIFT_SCHEMA, metric, metric_df)
