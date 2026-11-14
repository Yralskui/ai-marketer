"""Закрытое S3-хранилище оригиналов и Markdown."""

import asyncio
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import Settings


class ObjectStorage:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=60,
            ),
        )

    async def prepare(self) -> None:
        try:
            await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                raise
            try:
                await asyncio.to_thread(self.client.create_bucket, Bucket=self.bucket)
            except ClientError as error:
                if error.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                    raise

    async def upload(self, path: Path, key: str) -> None:
        await asyncio.to_thread(self.client.upload_file, str(path), self.bucket, key)

    async def download(self, key: str, path: Path) -> None:
        await asyncio.to_thread(self.client.download_file, self.bucket, key, str(path))

    async def put_markdown(self, key: str, content: str) -> None:
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=content.encode(),
            ContentType="text/markdown; charset=utf-8",
        )

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)
