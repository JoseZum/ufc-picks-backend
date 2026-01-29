"""
Tests para el servicio S3

Estos tests validan que el servicio S3 funcione correctamente:
- Generación de keys con naming conventions correctos
- Validación de configuración
- Manejo de modos (s3 vs cache)
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from app.services.s3_service import S3Service, S3NotConfiguredError, S3WriteNotAllowedError


class TestS3Service:
    """Tests para S3Service"""

    @patch('app.services.s3_service.get_settings')
    def test_generate_event_image_key(self, mock_settings):
        """Validar que las keys de eventos usen el formato correcto"""
        # Configurar mock
        mock_settings.return_value = Mock(
            image_source_mode="s3",
            aws_access_key_id="test",
            aws_secret_access_key="test",
            aws_s3_bucket="test-bucket",
            aws_cloudfront_domain="test.cloudfront.net"
        )

        service = S3Service()

        # Test con número de evento
        key = service.generate_event_image_key(324)
        assert key == "events/ufc-324.jpg"

        # Test con extensión diferente
        key = service.generate_event_image_key(325, "png")
        assert key == "events/ufc-325.png"

    @patch('app.services.s3_service.get_settings')
    def test_generate_fighter_image_key(self, mock_settings):
        """Validar que las keys de peleadores usen el formato correcto"""
        mock_settings.return_value = Mock(
            image_source_mode="s3",
            aws_access_key_id="test",
            aws_secret_access_key="test",
            aws_s3_bucket="test-bucket"
        )

        service = S3Service()

        # Test con ID de peleador
        key = service.generate_fighter_image_key("123456")
        assert key == "fighters/123456.jpg"

        # Test con extensión diferente
        key = service.generate_fighter_image_key("789012", "png")
        assert key == "fighters/789012.png"

    @patch('app.services.s3_service.get_settings')
    def test_is_read_only_mode_cache(self, mock_settings):
        """Validar que el modo cache sea read-only"""
        mock_settings.return_value = Mock(
            image_source_mode="cache",
            aws_access_key_id="test",
            aws_secret_access_key="test",
            aws_s3_bucket="test-bucket"
        )

        service = S3Service()
        assert service.is_read_only is True

    @patch('app.services.s3_service.get_settings')
    def test_is_read_only_mode_s3(self, mock_settings):
        """Validar que el modo s3 NO sea read-only"""
        mock_settings.return_value = Mock(
            image_source_mode="s3",
            aws_access_key_id="test",
            aws_secret_access_key="test",
            aws_s3_bucket="test-bucket"
        )

        service = S3Service()
        assert service.is_read_only is False

    @patch('app.services.s3_service.get_settings')
    def test_invalid_mode_raises_error(self, mock_settings):
        """Validar que un modo inválido lance error"""
        mock_settings.return_value = Mock(
            image_source_mode="invalid",
            aws_access_key_id="test",
            aws_secret_access_key="test",
            aws_s3_bucket="test-bucket"
        )

        with pytest.raises(ValueError, match="IMAGE_SOURCE_MODE inválido"):
            S3Service()

    @patch('app.services.s3_service.get_settings')
    def test_get_cloudfront_url(self, mock_settings):
        """Validar generación correcta de URLs de CloudFront"""
        mock_settings.return_value = Mock(
            image_source_mode="s3",
            aws_cloudfront_domain="d6huioh3922nf.cloudfront.net",
            aws_access_key_id="test",
            aws_secret_access_key="test",
            aws_s3_bucket="test-bucket"
        )

        service = S3Service()

        url = service.get_cloudfront_url("events/ufc-324.jpg")
        assert url == "https://d6huioh3922nf.cloudfront.net/events/ufc-324.jpg"

    @patch('app.services.s3_service.get_settings')
    def test_get_cloudfront_url_no_domain(self, mock_settings):
        """Validar que retorne None si no hay dominio de CloudFront"""
        mock_settings.return_value = Mock(
            image_source_mode="s3",
            aws_cloudfront_domain=None,
            aws_access_key_id="test",
            aws_secret_access_key="test",
            aws_s3_bucket="test-bucket"
        )

        service = S3Service()

        url = service.get_cloudfront_url("events/ufc-324.jpg")
        assert url is None

    @patch('app.services.s3_service.get_settings')
    def test_extract_key_from_cloudfront_url(self, mock_settings):
        """Validar extracción de key desde URL de CloudFront"""
        mock_settings.return_value = Mock(
            image_source_mode="s3",
            aws_access_key_id="test",
            aws_secret_access_key="test",
            aws_s3_bucket="test-bucket"
        )

        service = S3Service()

        cloudfront_url = "https://d6huioh3922nf.cloudfront.net/events/ufc-324.jpg"
        key = service.extract_key_from_cloudfront_url(cloudfront_url)
        assert key == "events/ufc-324.jpg"

        # Test con URL sin https
        cloudfront_url = "http://d6huioh3922nf.cloudfront.net/fighters/123456.jpg"
        key = service.extract_key_from_cloudfront_url(cloudfront_url)
        assert key == "fighters/123456.jpg"

    @patch('app.services.s3_service.get_settings')
    @patch('app.services.s3_service.boto3')
    async def test_upload_raises_error_in_cache_mode(self, mock_boto3, mock_settings):
        """Validar que subir a S3 en modo cache lance error"""
        mock_settings.return_value = Mock(
            image_source_mode="cache",
            aws_access_key_id="test",
            aws_secret_access_key="test",
            aws_s3_bucket="test-bucket"
        )

        service = S3Service()

        with pytest.raises(S3WriteNotAllowedError):
            await service.upload_image(
                s3_key="test.jpg",
                image_data=b"fake image data"
            )
