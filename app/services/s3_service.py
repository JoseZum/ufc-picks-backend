"""
Servicio de S3 y CloudFront para gestión de imágenes

Este servicio centraliza toda la lógica de interacción con AWS S3 y CloudFront.
Maneja dos modos de operación:
- Modo S3: lectura y escritura (sube imágenes faltantes automáticamente)
- Modo cache: solo lectura (nunca modifica S3, útil para staging/preview)

El backend solo trabaja con "keys" (rutas dentro del bucket), nunca con URLs completas.
CloudFront se encarga de servir las imágenes públicamente.
"""

import hashlib
import re
from typing import Literal, Optional
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
    """
    Servicio para interactuar con AWS S3 y CloudFront

    Responsabilidades:
    - Generar keys consistentes para eventos y peleadores
    - Verificar existencia de imágenes en S3
    - Subir imágenes a S3 (solo en modo s3)
    - Generar URLs de CloudFront para servir las imágenes
    """

    def __init__(self):
        self.settings = get_settings()
        self._s3_client = None

        # Validar que el modo de origen sea válido
        if self.settings.image_source_mode not in ["s3", "cache"]:
            raise ValueError(
                f"IMAGE_SOURCE_MODE inválido: {self.settings.image_source_mode}. "
                "Debe ser 's3' o 'cache'"
            )

    @property
    def s3_client(self):
        """
        Cliente de S3 lazy-loaded

        Solo se inicializa cuando realmente se necesita, y se cachea para
        reutilizar la misma conexión. Lanza error si S3 no está configurado.
        """
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
                )

        return self._s3_client

    @property
    def is_read_only(self) -> bool:
        """
        Indica si estamos en modo solo lectura (cache)

        En modo cache, NUNCA se debe escribir en S3. Útil para ambientes
        que no deben modificar el bucket de producción.
        """
        return self.settings.image_source_mode == "cache"

    def generate_event_image_key(self, event_id: int, file_ext: str = "jpg") -> str:
        """
        Genera la key S3 para la imagen de un evento

        Naming convention para eventos numerados:
        - events/ufc-324.jpg
        - events/ufc-325.jpg

        Args:
            event_id: ID numérico del evento
            file_ext: Extensión del archivo (default: jpg)

        Returns:
            Key S3 en formato: "events/ufc-{numero}.{ext}"
        """
        return f"events/ufc-{event_id}.{file_ext}"

    def generate_fighter_image_key(self, fighter_id: str, file_ext: str = "jpg") -> str:
        """
        Genera la key S3 para la imagen de un peleador

        Naming convention:
        - fighters/{fighter_id}.jpg
        - Ejemplo: fighters/123456.jpg

        Args:
            fighter_id: ID del peleador (puede ser string o int)
            file_ext: Extensión del archivo (default: jpg)

        Returns:
            Key S3 en formato: "fighters/{fighter_id}.{ext}"
        """
        return f"fighters/{fighter_id}.{file_ext}"

    def generate_tapology_cache_key(self, tapology_path: str) -> str:
        """
        Genera la key S3 para una imagen de Tapology (cache de proxy)

        Para imágenes genéricas de Tapology que no sean eventos/fighters específicos,
        usamos un hash del path para evitar colisiones.

        Args:
            tapology_path: Path de la imagen en Tapology
                          Ej: /poster_images/135755/profile/xxx.jpg

        Returns:
            Key S3 en formato: "tapology-images/{hash}.{ext}"
        """
        file_ext = tapology_path.split(".")[-1] if "." in tapology_path else "jpg"
        cache_key = hashlib.md5(tapology_path.encode()).hexdigest()
        return f"tapology-images/{cache_key}.{file_ext}"

    async def image_exists(self, s3_key: str) -> bool:
        """
        Verifica si una imagen existe en S3

        Hace una llamada head_object que es más eficiente que descargar
        el objeto completo. Útil para decidir si hay que subir o no.

        Args:
            s3_key: Key de la imagen en S3

        Returns:
            True si la imagen existe, False si no existe

        Raises:
            S3NotConfiguredError: Si S3 no está configurado
        """
        try:
            self.s3_client.head_object(
                Bucket=self.settings.aws_s3_bucket,
                Key=s3_key
            )
            return True
        except self.s3_client.exceptions.NoSuchKey:
            return False
        except Exception:
            # Cualquier otro error (permisos, red, etc) lo consideramos como "no existe"
            # para que el sistema intente la operación normal
            return False

    async def upload_image(
        self,
        s3_key: str,
        image_data: bytes,
        content_type: str = "image/jpeg",
        metadata: Optional[dict] = None
    ) -> None:
        """
        Sube una imagen a S3

        Solo funciona en modo "s3". En modo "cache" lanza error ya que no
        debe haber escrituras.

        Args:
            s3_key: Key donde guardar la imagen en S3
            image_data: Bytes de la imagen
            content_type: MIME type de la imagen (default: image/jpeg)
            metadata: Metadata opcional para guardar con la imagen

        Raises:
            S3WriteNotAllowedError: Si estamos en modo cache (solo lectura)
            S3NotConfiguredError: Si S3 no está configurado
        """
        if self.is_read_only:
            raise S3WriteNotAllowedError(
                f"No se puede escribir en S3 en modo '{self.settings.image_source_mode}'. "
                "Cambia IMAGE_SOURCE_MODE a 's3' para habilitar escritura."
            )

        # Preparar parámetros de upload
        upload_params = {
            "Bucket": self.settings.aws_s3_bucket,
            "Key": s3_key,
            "Body": BytesIO(image_data),
            "ContentType": content_type,
            "CacheControl": "public, max-age=31536000",  # 1 año - las imágenes no cambian
        }

        # Agregar metadata si existe
        if metadata:
            upload_params["Metadata"] = metadata

        # Subir a S3
        self.s3_client.put_object(**upload_params)

    async def get_image(self, s3_key: str) -> tuple[bytes, str]:
        """
        Descarga una imagen desde S3

        Args:
            s3_key: Key de la imagen en S3

        Returns:
            Tupla de (image_data, content_type)

        Raises:
            S3NotConfiguredError: Si S3 no está configurado
            botocore.exceptions.NoSuchKey: Si la imagen no existe
        """
        response = self.s3_client.get_object(
            Bucket=self.settings.aws_s3_bucket,
            Key=s3_key
        )
        image_data = response['Body'].read()
        content_type = response.get('ContentType', 'image/jpeg')
        return image_data, content_type

    def get_cloudfront_url(self, s3_key: str) -> Optional[str]:
        """
        Genera la URL pública de CloudFront para una imagen

        CloudFront actúa como CDN frente a S3. SIEMPRE debemos usar CloudFront
        en lugar de URLs directas de S3 por:
        - Mejor rendimiento (edge locations)
        - S3 bucket es privado, CloudFront lo expone públicamente
        - Costos de transferencia más bajos

        Args:
            s3_key: Key de la imagen en S3
                   Ej: "events/ufc-324.jpg"

        Returns:
            URL completa de CloudFront si está configurado, None si no
            Ej: "https://d6huioh3922nf.cloudfront.net/events/ufc-324.jpg"
        """
        if not self.settings.aws_cloudfront_domain:
            return None

        # Asegurar que el dominio no tenga https:// al inicio
        domain = self.settings.aws_cloudfront_domain.replace("https://", "").replace("http://", "")
        return f"https://{domain}/{s3_key}"

    def extract_key_from_cloudfront_url(self, cloudfront_url: str) -> Optional[str]:
        """
        Extrae la key S3 desde una URL de CloudFront

        Útil para operaciones inversas cuando tenemos una URL y necesitamos
        la key para verificar existencia, etc.

        Args:
            cloudfront_url: URL de CloudFront
                           Ej: "https://d6huioh3922nf.cloudfront.net/events/ufc-324.jpg"

        Returns:
            La key S3 extraída, o None si no se pudo parsear
            Ej: "events/ufc-324.jpg"
        """
        if not cloudfront_url:
            return None

        # Intentar extraer todo después del dominio
        pattern = r"https?://[^/]+/(.+)"
        match = re.search(pattern, cloudfront_url)
        if match:
            return match.group(1)

        return None

    def is_cloudfront_configured(self) -> bool:
        """
        Verifica si CloudFront está configurado correctamente

        Retorna False si:
        - No hay dominio configurado
        - El dominio es el de ejemplo
        """
        if not self.settings.aws_cloudfront_domain:
            return False

        # Dominios de ejemplo que no deben usarse
        example_domains = [
            "d111111abcdef8.cloudfront.net",
            "dXXXXXXXXXXXXX.cloudfront.net",
            "example.cloudfront.net",
        ]

        domain = self.settings.aws_cloudfront_domain.replace("https://", "").replace("http://", "")
        return domain not in example_domains

    def get_event_poster_cloudfront_url(self, event_id: int) -> Optional[str]:
        """
        Obtiene la URL de CloudFront para un poster de evento específico

        SOLO para eventos que sabemos que tienen poster en S3 bajo ufc-posters/ufc{id}.jpeg
        Si no está en S3 o CloudFront no está configurado, retorna None.

        Args:
            event_id: ID numérico del evento

        Returns:
            URL de CloudFront si existe el poster en S3, None si no
        """
        if not self.is_cloudfront_configured():
            return None

        # El formato en S3 es: ufc-posters/ufc{numero}.jpeg
        s3_key = f"ufc-posters/ufc{event_id}.jpeg"

        # Generar URL de CloudFront
        return self.get_cloudfront_url(s3_key)


# Instancia singleton del servicio
_s3_service_instance: Optional[S3Service] = None


def get_s3_service() -> S3Service:
    """
    Retorna la instancia singleton del servicio S3

    Usamos singleton para:
    - Reutilizar la conexión a S3 (evitar crear múltiples clientes)
    - Mantener configuración consistente en toda la app
    """
    global _s3_service_instance
    if _s3_service_instance is None:
        _s3_service_instance = S3Service()
    return _s3_service_instance
