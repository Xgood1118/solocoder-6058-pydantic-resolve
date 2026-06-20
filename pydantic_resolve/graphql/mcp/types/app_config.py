"""App configuration for MCP server."""

from typing import Any, Awaitable, Callable
import importlib
import os

from pydantic import BaseModel, Field

from pydantic_resolve.utils.er_diagram import ErDiagram


class YamlAppConfig(BaseModel):
    """Configuration for a GraphQL application loaded from YAML.

    This is used for hot-reload scenarios where entities and loaders
    are specified as import paths rather than direct Python objects.

    Attributes:
        name: Application name (required)
        entity_set: Import path to entity set module or BaseEntity class
            (e.g. "myapp.entities:BaseEntity" or "myapp.entities")
        loader_path: Import path to loader module (e.g. "myapp.loaders")
        description: Optional application description
        enable_from_attribute: Enable Pydantic from_attributes mode
    """

    name: str
    entity_set: str
    loader_path: str | None = None
    description: str | None = None
    enable_from_attribute: bool = False


class AppConfig(BaseModel):
    """Configuration for a GraphQL application in MCP server.

    Attributes:
        name: Application name (required)
        er_diagram: ErDiagram instance containing entity definitions (required)
        description: Optional application description
        query_description: Optional description for Query type
        mutation_description: Optional description for Mutation type
        enable_from_attribute_in_type_adapter: Enable Pydantic from_attributes mode.
            Allows loaders to return Pydantic instances instead of dictionaries.
            Default is False.
        context_extractor: Optional callback that extracts request-scoped context
            (e.g. user identity from Authorization header). Receives the FastMCP
            Context object and returns a dict passed as ``context=`` to
            ``handler.execute()``. Can be sync or async.
    """

    model_config = {"arbitrary_types_allowed": True}

    name: str
    er_diagram: ErDiagram
    description: str | None = None
    query_description: str | None = None
    mutation_description: str | None = None
    enable_from_attribute_in_type_adapter: bool = False
    context_extractor: Callable[[Any], dict | Awaitable[dict]] | None = None


def _import_entity_set(entity_set_path: str) -> ErDiagram:
    """Import an entity set from a module path.

    Supports:
    - "module.path:ClassName" - imports specific class and calls get_diagram()
    - "module.path" - imports module and looks for BaseEntity or get_diagram()

    Args:
        entity_set_path: Import path to entity set

    Returns:
        ErDiagram instance

    Raises:
        ImportError: If module or class cannot be imported
        AttributeError: If no ErDiagram can be obtained from the path
    """
    if ':' in entity_set_path:
        module_path, class_name = entity_set_path.rsplit(':', 1)
        module = importlib.import_module(module_path)
        entity_class = getattr(module, class_name)
        if hasattr(entity_class, 'get_diagram'):
            return entity_class.get_diagram()
        raise AttributeError(f"'{class_name}' has no 'get_diagram()' method")
    else:
        module = importlib.import_module(entity_set_path)
        if hasattr(module, 'BaseEntity') and hasattr(module.BaseEntity, 'get_diagram'):
            return module.BaseEntity.get_diagram()
        if hasattr(module, 'get_diagram'):
            return module.get_diagram()
        raise AttributeError(
            f"Module '{entity_set_path}' has no 'BaseEntity.get_diagram()' or 'get_diagram()' method"
        )


def _import_loaders(loader_path: str | None) -> None:
    """Import loader module to ensure loaders are registered.

    Args:
        loader_path: Import path to loader module, or None
    """
    if loader_path:
        importlib.import_module(loader_path)


def load_app_configs_from_yaml(yaml_path: str) -> list[AppConfig]:
    """Load AppConfig list from a YAML configuration file.

    The YAML file should have the structure::

        apps:
          - name: blog
            entity_set: myapp.blog.entities:BaseEntity
            loader_path: myapp.blog.loaders
            description: Blog system with users and posts
            enable_from_attribute: true
          - name: shop
            entity_set: myapp.shop.entities
            loader_path: myapp.shop.loaders

    Args:
        yaml_path: Path to YAML configuration file

    Returns:
        List of AppConfig instances

    Raises:
        ImportError: If yaml package is not installed
        FileNotFoundError: If YAML file does not exist
        ValueError: If YAML format is invalid
    """
    try:
        import yaml
    except ImportError:
        raise ImportError(
            "PyYAML is required for YAML config loading. "
            "Install with: pip install pyyaml"
        )

    if not os.path.exists(yaml_path):
        raise FileNotFoundError(f"YAML config file not found: {yaml_path}")

    with open(yaml_path, 'r', encoding='utf-8') as f:
        config_data = yaml.safe_load(f)

    if not isinstance(config_data, dict) or 'apps' not in config_data:
        raise ValueError(
            "Invalid YAML format. Expected 'apps' key at top level."
        )

    yaml_configs = [YamlAppConfig(**item) for item in config_data['apps']]

    app_configs: list[AppConfig] = []
    for yaml_cfg in yaml_configs:
        if yaml_cfg.loader_path:
            _import_loaders(yaml_cfg.loader_path)
        er_diagram = _import_entity_set(yaml_cfg.entity_set)
        app_configs.append(AppConfig(
            name=yaml_cfg.name,
            er_diagram=er_diagram,
            description=yaml_cfg.description,
            enable_from_attribute_in_type_adapter=yaml_cfg.enable_from_attribute,
        ))

    return app_configs
