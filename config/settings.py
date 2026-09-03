"""
config/settings.py
Configuration management for God Node V2

Handles environment-based settings, API keys, security, and service lifecycle.
"""

import os
import logging
from typing import Optional, Dict, List
from enum import Enum

logger = logging.getLogger("GodNode.Config")

class EnvironmentMode(Enum):
    """Deployment environment modes"""
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"

class GodNodeConfig:
    """
    Centralized configuration for God Node V2.
    Loads from environment variables and validates at startup.
    """
    
    def __init__(self):
        # Deployment mode
        env_str = os.getenv("GOD_ENV", "development").lower()
        try:
            self.environment = EnvironmentMode(env_str)
        except ValueError:
            self.environment = EnvironmentMode.DEVELOPMENT
            logger.warning(f"Invalid GOD_ENV value '{env_str}'. Using development mode.")
        
        # Security: Master PIN configuration
        self.master_pin = os.getenv("GOD_MASTER_PIN")
        self.require_secure_pin = self.environment == EnvironmentMode.PRODUCTION
        
        # API Provider configuration (non-blocking)
        self.api_providers = self._load_api_providers()
        
        # HTTP Client configuration
        self.http_timeout_seconds = int(os.getenv("GOD_HTTP_TIMEOUT", "60"))
        self.http_pool_size = int(os.getenv("GOD_HTTP_POOL_SIZE", "50"))
        
        # Task management
        self.max_tasks_registry_size = int(os.getenv("GOD_MAX_TASKS", "10000"))
        self.task_ttl_seconds = int(os.getenv("GOD_TASK_TTL", "3600"))
        
        # Rate limiting
        self.rate_limit_enabled = os.getenv("GOD_RATE_LIMIT", "true").lower() == "true"
        self.rate_limit_requests_per_minute = int(os.getenv("GOD_RATE_LIMIT_RPM", "60"))
        
        # Logging
        self.log_level = os.getenv("GOD_LOG_LEVEL", "INFO")
        
        # Validation
        self._validate_configuration()
    
    def _load_api_providers(self) -> Dict[str, List[str]]:
        """Load API provider keys from environment variables (non-blocking)"""
        providers = {}
        
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            providers["openai"] = [openai_key]
            logger.info("✅ OpenAI provider configured")
        
        google_key = os.getenv("GOOGLE_API_KEY")
        if google_key:
            providers["gemini"] = [google_key]
            logger.info("✅ Google Gemini provider configured")
        
        return providers
    
    def _validate_configuration(self) -> None:
        """Validate critical configuration at startup"""
        
        # Production mode requires secure PIN
        if self.require_secure_pin and not self.master_pin:
            raise ValueError(
                "FATAL: Production mode requires GOD_MASTER_PIN environment variable. "
                "Set GOD_ENV=development to use development mode."
            )
        
        # Development mode warning
        if self.environment == EnvironmentMode.DEVELOPMENT and not self.master_pin:
            logger.warning(
                "⚠️  DEVELOPMENT MODE: Using insecure default PIN '7777'. "
                "Set GOD_MASTER_PIN for custom value. "
                "DO NOT use in production."
            )
        
        logger.info(f"✅ Configuration validated for {self.environment.value} mode")
    
    def get_api_providers(self) -> Dict[str, List[str]]:
        """Get configured API providers"""
        return self.api_providers.copy()
    
    def has_provider(self, provider_name: str) -> bool:
        """Check if a provider is configured"""
        return provider_name in self.api_providers
    
    def get_master_pin(self) -> str:
        """Get the master PIN (with safe defaults for dev mode)"""
        if self.master_pin:
            return self.master_pin
        if self.environment == EnvironmentMode.DEVELOPMENT:
            return "7777"
        raise ValueError("Master PIN not configured")
    
    def is_production(self) -> bool:
        """Check if running in production mode"""
        return self.environment == EnvironmentMode.PRODUCTION
    
    def is_development(self) -> bool:
        """Check if running in development mode"""
        return self.environment == EnvironmentMode.DEVELOPMENT


# Global config instance
god_config = GodNodeConfig()
