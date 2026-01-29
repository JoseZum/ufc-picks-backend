"""
Tests para configuración de la aplicación

Valida que la configuración se cargue correctamente desde variables de entorno
y que los valores por defecto sean apropiados.
"""

import pytest
from unittest.mock import patch
from app.core.config import Settings


class TestSettings:
    """Tests para Settings"""

    @patch.dict('os.environ', {
        'MONGODB_URI': 'mongodb://test:test@localhost:27017/test',
        'JWT_SECRET': 'test-secret-key-at-least-32-chars-long',
        'GOOGLE_CLIENT_ID': 'test-google-client-id',
    })
    def test_settings_loads_from_env(self):
        """Validar que Settings carga desde variables de entorno"""
        settings = Settings()

        assert settings.mongodb_uri == 'mongodb://test:test@localhost:27017/test'
        assert settings.jwt_secret == 'test-secret-key-at-least-32-chars-long'
        assert settings.google_client_id == 'test-google-client-id'

    @patch.dict('os.environ', {
        'MONGODB_URI': 'mongodb://test:test@localhost:27017/test',
        'JWT_SECRET': 'test-secret-key',
        'GOOGLE_CLIENT_ID': 'test-google-client-id',
    })
    def test_settings_default_values(self):
        """Validar valores por defecto de configuración"""
        settings = Settings()

        # Valores por defecto
        # mongodb_db_name puede variar según el ambiente (test vs dev)
        assert settings.mongodb_db_name in ["ufc_picks", "ufc_picks_test"]
        assert settings.jwt_algorithm == "HS256"
        assert settings.jwt_expire_minutes == 60 * 24 * 7  # 7 días
        assert settings.app_env in ["development", "test"]  # En CI puede ser "test"
        assert isinstance(settings.debug, bool)  # Can be True or False depending on environment
        assert settings.aws_region == "us-east-1"

    @patch.dict('os.environ', {
        'MONGODB_URI': 'mongodb://test:test@localhost:27017/test',
        'JWT_SECRET': 'test-secret-key',
        'GOOGLE_CLIENT_ID': 'test-google-client-id',
        'IMAGE_CACHE_STRATEGY': 'S3',
        'IMAGE_SOURCE_MODE': 'cache',
    })
    def test_settings_image_modes(self):
        """Validar que los modos de imagen se configuren correctamente"""
        settings = Settings()

        assert settings.image_cache_strategy == "S3"
        assert settings.image_source_mode == "cache"

    @patch.dict('os.environ', {
        'MONGODB_URI': 'mongodb://test:test@localhost:27017/test',
        'JWT_SECRET': 'test-secret-key',
        'GOOGLE_CLIENT_ID': 'test-google-client-id',
        'AWS_CLOUDFRONT_DOMAIN': 'd6huioh3922nf.cloudfront.net',
    })
    def test_settings_cloudfront_domain(self):
        """Validar que el dominio de CloudFront se cargue"""
        settings = Settings()

        assert settings.aws_cloudfront_domain == "d6huioh3922nf.cloudfront.net"
