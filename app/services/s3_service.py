"""Acceso a S3 y CloudFront para las imágenes.

IMAGE_SOURCE_MODE elige el modo: "s3" lee y escribe, "cache" solo lee y nunca
toca el bucket. El backend maneja keys, no URLs: CloudFront las sirve.
"""

import hashlib
import re
from io import BytesIO

from app.core.config import get_settings


class S3ServiceError(Exception):
    """Error base para excepciones del servicio S3"""
    pass


class S3NotConfiguredError(S3ServiceError):
    """Se intentó usar S3 sin configurar las credenciales necesarias"""
    pass


class S3WriteNotAllowedError(S3ServiceError):
    """Se intentó escribir en S3 estando en modo cache (solo lectura)"""
    pass


class S3Service:
    """Genera keys, sube imágenes y arma las URLs de CloudFront."""

    def __init__(self):
        self.settings = get_settings()
        self._s3_client = None

        if self.settings.image_source_mode not in ["s3", "cache"]:
            raise ValueError(
                f"IMAGE_SOURCE_MODE inválido: {self.settings.image_source_mode}. "
                "Debe ser 's3' o 'cache'"
            )

    @property
    def s3_client(self):
        """Cliente boto3, creado la primera vez que se usa y reutilizado."""
        if self._s3_client is None:
            if not all([
                self.settings.aws_access_key_id,
                self.settings.aws_secret_access_key,
                self.settings.aws_s3_bucket
            ]):
                raise S3NotConfiguredError(
                    "S3 no está configurado. Faltan: AWS_ACCESS_KEY_ID, "
                    "AWS_SECRET_ACCESS_KEY o AWS_S3_BUCKET"
                )

            try:
                import boto3
                self._s3_client = boto3.client(
                    's3',
                    aws_access_key_id=self.settings.aws_access_key_id,
                    aws_secret_access_key=self.settings.aws_secret_access_key,
                    region_name=self.settings.aws_region
                )
            except ImportError:
                raise S3NotConfiguredError(
                    "boto3 no está instalado. Instalar con: pip install boto3"
                ) from None

        return self._s3_client

    @property
    def is_read_only(self) -> bool:
        """En modo cache nunca se escribe: lo usan los entornos que comparten
        el bucket de producción."""
        return self.settings.image_source_mode == "cache"

    def generate_event_image_key(self, event_id: int, file_ext: str = "jpg") -> str:
        """Key del poster de un evento, tipo `events/ufc-324.jpg`."""
        return f"events/ufc-{event_id}.{file_ext}"

    def generate_fighter_image_key(self, fighter_id: str, file_ext: str = "jpg") -> str:
        """Key de la foto de un peleador, tipo `fighters/123456.jpg`."""
        return f"fighters/{fighter_id}.{file_ext}"

    def generate_tapology_cache_key(self, tapology_path: str) -> str:
        """Key para el cache del proxy. El path va hasheado para evitar
        colisiones entre imágenes de Tapology que no son de evento ni peleador."""
        file_ext = tapology_path.split(".")[-1] if "." in tapology_path else "jpg"
        cache_key = hashlib.md5(tapology_path.encode()).hexdigest()
        return f"tapology-images/{cache_key}.{file_ext}"

    async def image_exists(self, s3_key: str) -> bool:
        """Comprueba la existencia con head_object, sin descargar el objeto."""
        try:
            self.s3_client.head_object(
                Bucket=self.settings.aws_s3_bucket,
                Key=s3_key
            )
            return True
        except self.s3_client.exceptions.NoSuchKey:
            return False
        except Exception:
            # Permisos o red caídos cuentan como "no existe" para que el
            # flujo normal siga y reintente la subida.
            return False

    async def upload_image(
        self,
        s3_key: str,
        image_data: bytes,
        content_type: str = "image/jpeg",
        metadata: dict | None = None
    ) -> None:
        """Sube una imagen. Falla en modo cache, que es de solo lectura."""
        if self.is_read_only:
            raise S3WriteNotAllowedError(
                f"No se puede escribir en S3 en modo '{self.settings.image_source_mode}'. "
                "Cambia IMAGE_SOURCE_MODE a 's3' para habilitar escritura."
            )

        upload_params = {
            "Bucket": self.settings.aws_s3_bucket,
            "Key": s3_key,
            "Body": BytesIO(image_data),
            "ContentType": content_type,
            "CacheControl": "public, max-age=31536000",  # 1 año: no cambian
        }
        if metadata:
            upload_params["Metadata"] = metadata

        self.s3_client.put_object(**upload_params)

    async def get_image(self, s3_key: str) -> tuple[bytes, str]:
        """Descarga una imagen y devuelve (bytes, content_type)."""
        response = self.s3_client.get_object(
            Bucket=self.settings.aws_s3_bucket,
            Key=s3_key
        )
        image_data = response['Body'].read()
        content_type = response.get('ContentType', 'image/jpeg')
        return image_data, content_type

    def get_cloudfront_url(self, s3_key: str) -> str | None:
        """URL pública de una key. Siempre por CloudFront: el bucket es
        privado y solo el CDN lo expone."""
        if not self.settings.aws_cloudfront_domain:
            return None

        domain = self.settings.aws_cloudfront_domain.replace("https://", "").replace("http://", "")
        return f"https://{domain}/{s3_key}"

    def extract_key_from_cloudfront_url(self, cloudfront_url: str) -> str | None:
        """Operación inversa de `get_cloudfront_url`: devuelve la key o None."""
        if not cloudfront_url:
            return None

        pattern = r"https?://[^/]+/(.+)"
        match = re.search(pattern, cloudfront_url)
        if match:
            return match.group(1)

        return None

    def is_cloudfront_configured(self) -> bool:
        """False si falta el dominio o si quedó el de ejemplo del .env."""
        if not self.settings.aws_cloudfront_domain:
            return False

        example_domains = [
            "d111111abcdef8.cloudfront.net",
            "dXXXXXXXXXXXXX.cloudfront.net",
            "example.cloudfront.net",
        ]

        domain = self.settings.aws_cloudfront_domain.replace("https://", "").replace("http://", "")
        return domain not in example_domains

_s3_service_instance: S3Service | None = None


def get_s3_service() -> S3Service:
    """Instancia única, para no abrir un cliente de boto3 por llamada."""
    global _s3_service_instance
    if _s3_service_instance is None:
        _s3_service_instance = S3Service()
    return _s3_service_instance
