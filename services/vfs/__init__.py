"""
VirtualFileSystem package - bash-like virtual filesystem on COS

Submodules:
- tokens: bash tokenizer
- parser: bash parser (tokens → AST)
- executor: command executor (AST → CommandResult)
- registry: command registry (maps command names to implementations)
- builtin: builtin command implementations
- paths: COS path mapping + path validation
- cos_client: COS get/put/delete/list wrapper
"""

from .paths import (
    PathResolver,
    validate_filename,
    parse_cos_date,
    gen_inode,
    get_mode_from_name,
    format_date,
    format_size,
    classify_suffix,
    mode_to_octal,
    mode_to_perm_string,
    parse_cut_fields,
)

from .cos_client import CosClient

__all__ = [
    "PathResolver",
    "CosClient",
    "validate_filename",
    "parse_cos_date",
    "gen_inode",
    "get_mode_from_name",
    "format_date",
    "format_size",
    "classify_suffix",
    "mode_to_octal",
    "mode_to_perm_string",
    "parse_cut_fields",
]
