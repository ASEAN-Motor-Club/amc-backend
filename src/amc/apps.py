from django.apps import AppConfig

from django_asgi_lifespan.register import register_lifespan_manager

from .context import (
    aiohttp_lifespan_manager,
)

class AMCConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'amc'

    def ready(self):
        from amc.command_framework import registry
        registry.autodiscover('amc.commands')
        register_lifespan_manager(context_manager=aiohttp_lifespan_manager)
