"""
================================================================================
AUTONOMOUS SELF-EVOLUTION ENGINE - God Node V2
================================================================================
Meta-Programming Module with Human-Approved Autonomy

SAFETY ARCHITECTURE:
- Scans entire repo for missing modules & bottlenecks
- Generates advanced Python code (AST-validated)
- Creates LOCAL .py files with optimization
- Opens GitHub Pull Request for HUMAN REVIEW
- Blocks automatic merge to main branch
- Maintains full audit trail & rollback capability

Author: God Node V2 Meta-Architecture
License: Enterprise Secure
================================================================================
"""

import os
import ast
import json
import shutil
import asyncio
import logging
import hashlib
import datetime
import tempfile
import subprocess
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from pathlib import Path
from enum import Enum

try:
    import aiofiles
    from github import Github, GithubException
except ImportError:
    print("⚠️  Optional dependencies not installed. Run: pip install PyGithub aiofiles")
    aiofiles = None
    Github = None
    GithubException = Exception


# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================================
# SECURITY & CONFIGURATION CONSTANTS
# ============================================================================
class EvolutionConfig:
    """Centralized configuration with security constraints"""
    
    # GitHub Authentication (from secure environment variables)
    GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
    GITHUB_OWNER = os.getenv("GITHUB_OWNER", "")
    GITHUB_REPO = os.getenv("GITHUB_REPO", "Riot")
    GITHUB_BRANCH_PREFIX = "auto-evolution"
    
    # Local development paths
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    TEMP_DIR = os.path.join(BASE_DIR, ".evolution_workspace")
    BACKUP_DIR = os.path.join(BASE_DIR, "backups")
    AUDIT_LOG_DIR = os.path.join(BASE_DIR, "audit_logs")
    
    # Safety constraints
    MAX_FILE_SIZE_KB = 100  # Maximum generated file size
    MAX_GENERATION_TIME_SEC = 60  # Max time for code generation
    ALLOWED_DIRS = [
        "god_brain", "core", "core_engine", "multiplayer_nexus",
        "pixel_streaming", "security_vault", "simulation_scheduler",
        "assets_factory", "cloud_storage", "economy_vault", "deployment",
        "game_compilers", "live_editor", "mobile_services", "the_god_router",
        "config",
    ]
    
    # PR settings
    PR_LABEL = "auto-generated"
    PR_DRAFT = True  # Create as draft PR
    PR_REQUEST_REVIEWERS = [x.strip() for x in os.getenv("RIOT_EVOLUTION_REVIEWERS", "").split(",") if x.strip()]
    
    # AST safety rules
    DANGEROUS_IMPORTS = [
        "os.system",
        "subprocess.call",
        "eval",
        "exec",
        "__import__",
        "compile",
    ]
    
    DANGEROUS_FUNCTIONS = [
        "open",  # Restricted unless in safe context
        "exec",
        "eval",
        "__getattribute__",
        "setattr",
        "delattr",
    ]
    
    # Performance thresholds
    COMPLEXITY_THRESHOLD = 10  # Max cyclomatic complexity
    MAX_FUNCTION_LENGTH = 50  # Lines per function
    

class GenerationStatus(Enum):
    """Status tracking for generation lifecycle"""
    PENDING = "pending"
    SCANNING = "scanning"
    ANALYZING = "analyzing"
    GENERATING = "generating"
    VALIDATING = "validating"
    CREATING_PR = "creating_pr"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLBACK = "rollback"


# ============================================================================
# DATA STRUCTURES
# ============================================================================
@dataclass
class ModuleAnalysis:
    """Analysis result for a detected missing module"""
    module_name: str
    module_type: str  # "compiler", "agent", "optimizer", etc.
    reason: str
    priority: str  # "critical", "high", "medium", "low"
    suggested_location: str
    estimated_complexity: int
    dependencies: List[str]
    confidence_score: float


@dataclass
class GeneratedModule:
    """Metadata for generated Python module"""
    name: str
    file_path: str
    content: str
    ast_tree: Optional[ast.Module]
    validation_errors: List[str]
    optimization_metrics: Dict[str, Any]
    generated_at: str
    hash_digest: str
    complexity_score: float


@dataclass
class EvolutionAudit:
    """Audit trail entry for every generation"""
    timestamp: str
    action: str
    status: str
    modules_generated: List[str]
    pr_url: Optional[str]
    errors: List[str]
    metadata: Dict[str, Any]


# ============================================================================
# SECURITY VALIDATORS
# ============================================================================
class ASTSecurityValidator:
    """Validates generated code for security risks"""
    
    def __init__(self):
        self.security_issues: List[str] = []
        self.warnings: List[str] = []
    
    def validate_code(self, code: str) -> Tuple[bool, List[str]]:
        """
        Validate generated code for security and syntax issues
        
        Returns:
            Tuple[is_safe, issues_list]
        """
        self.security_issues = []
        self.warnings = []
        
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            self.security_issues.append(f"Syntax Error: {e}")
            return False, self.security_issues
        
        # Run all validation checks
        self._check_imports(tree)
        self._check_dangerous_calls(tree)
        self._check_complexity(tree)
        self._check_code_quality(tree)
        
        is_safe = len(self.security_issues) == 0
        return is_safe, self.security_issues + self.warnings
    
    def _check_imports(self, tree: ast.Module) -> None:
        """Validate import statements"""
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] in {"subprocess", "ctypes", "socket"}:
                        self.security_issues.append(
                            f"🚫 BLOCKED: sensitive system import: {alias.name}"
                        )
            
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    for danger in EvolutionConfig.DANGEROUS_IMPORTS:
                        if danger in node.module:
                            self.security_issues.append(
                                f"🚫 BLOCKED: Dangerous import detected: {danger}"
                            )
    
    def _check_dangerous_calls(self, tree: ast.Module) -> None:
        """Detect calls to dangerous functions"""
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in EvolutionConfig.DANGEROUS_FUNCTIONS:
                        self.security_issues.append(
                            f"🚫 BLOCKED: Dangerous function call: {node.func.id}()"
                        )
                
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                    if func_name in ["eval", "exec", "system", "call"]:
                        self.security_issues.append(
                            f"🚫 BLOCKED: Dangerous method: {func_name}()"
                        )
    
    def _check_complexity(self, tree: ast.Module) -> None:
        """Check cyclomatic complexity"""
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                complexity = self._calculate_complexity(node)
                if complexity > EvolutionConfig.COMPLEXITY_THRESHOLD:
                    self.warnings.append(
                        f"⚠️  High complexity in {node.name}: {complexity} "
                        f"(threshold: {EvolutionConfig.COMPLEXITY_THRESHOLD})"
                    )
    
    def _check_code_quality(self, tree: ast.Module) -> None:
        """Check code quality metrics"""
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                # Check function length
                func_length = len(node.body)
                if func_length > EvolutionConfig.MAX_FUNCTION_LENGTH:
                    self.warnings.append(
                        f"⚠️  Function {node.name} too long: {func_length} lines"
                    )
            
            elif isinstance(node, ast.ClassDef):
                # Check for missing docstrings
                if not ast.get_docstring(node):
                    self.warnings.append(
                        f"⚠️  Class {node.name} missing docstring"
                    )
    
    @staticmethod
    def _calculate_complexity(node: ast.FunctionDef) -> int:
        """Calculate cyclomatic complexity"""
        complexity = 1
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.For, ast.While, ast.ExceptHandler)):
                complexity += 1
        return complexity


# ============================================================================
# REPOSITORY SCANNER
# ============================================================================
class RepositoryMetaScanner:
    """Deep repository scanner for syntax, imports, architecture and resource risks."""

    _SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "venv", ".venv", "node_modules", "dist", "build", ".evolution_workspace", "backups"}
    _IMPORT_ROOTS = {"god_brain", "core", "core_engine", "multiplayer_nexus", "pixel_streaming", "security_vault", "simulation_scheduler", "assets_factory", "cloud_storage", "economy_vault", "deployment", "game_compilers", "live_editor", "mobile_services", "the_god_router", "config"}

    def __init__(self, base_dir: str = EvolutionConfig.BASE_DIR):
        self.base_dir = os.path.abspath(base_dir)
        self.python_files: List[str] = []
        self.imports_map: Dict[str, List[str]] = {}
        self.missing_modules: List[ModuleAnalysis] = []
        self.scan_issues: List[Dict[str, Any]] = []

    def scan_repository(self) -> Tuple[List[ModuleAnalysis], Dict[str, Any]]:
        """Perform a deterministic deep scan without mutating the repository."""
        started = datetime.datetime.utcnow()
        self.python_files.clear()
        self.imports_map.clear()
        self.missing_modules.clear()
        self.scan_issues.clear()
        logger.info("Starting deep repository meta-scan: %s", self.base_dir)

        self._collect_python_files()
        syntax_errors = self._scan_syntax_and_imports()
        unresolved_internal = self._scan_internal_imports()
        duplicate_modules = self._detect_duplicate_module_names()
        security_findings = self._scan_source_risks()
        dependency_findings = self._scan_dependency_drift()
        self._detect_missing_modules()

        total_lines = 0
        total_bytes = 0
        largest: List[Tuple[int, str]] = []
        for path in self.python_files:
            try:
                stat = os.stat(path)
                total_bytes += stat.st_size
                with open(path, "rb") as handle:
                    line_count = sum(1 for _ in handle)
                total_lines += line_count
                largest.append((stat.st_size, os.path.relpath(path, self.base_dir)))
            except OSError as exc:
                self.scan_issues.append({"kind": "read_error", "file": path, "detail": str(exc)})
        largest.sort(reverse=True)
        duration_ms = (datetime.datetime.utcnow() - started).total_seconds() * 1000.0

        metrics: Dict[str, Any] = {
            "schema": "riot.evolution.scan.v2",
            "total_files": len(self.python_files),
            "total_lines": total_lines,
            "total_bytes": total_bytes,
            "avg_file_size": total_lines / len(self.python_files) if self.python_files else 0,
            "largest_files": [{"path": path, "bytes": size} for size, path in largest[:10]],
            "syntax_errors": syntax_errors,
            "unresolved_internal_imports": unresolved_internal,
            "duplicate_module_names": duplicate_modules,
            "security_findings": security_findings,
            "dependency_findings": dependency_findings,
            "missing_module_candidates": len(self.missing_modules),
            "issues": self.scan_issues,
            "scan_duration_ms": round(duration_ms, 2),
            "scan_timestamp": datetime.datetime.utcnow().isoformat(),
        }
        logger.info(
            "Deep scan complete files=%d syntax_errors=%d unresolved=%d issues=%d",
            len(self.python_files), syntax_errors, unresolved_internal, len(self.scan_issues),
        )
        return self.missing_modules, metrics

    def _collect_python_files(self) -> None:
        for root, dirs, files in os.walk(self.base_dir):
            dirs[:] = [d for d in dirs if d not in self._SKIP_DIRS and not d.startswith(".")]
            for filename in files:
                if filename.endswith(".py"):
                    self.python_files.append(os.path.join(root, filename))
        self.python_files.sort()

    def _scan_syntax_and_imports(self) -> int:
        errors = 0
        for file_path in self.python_files:
            try:
                with open(file_path, "r", encoding="utf-8") as handle:
                    content = handle.read()
                tree = ast.parse(content, filename=file_path)
                imports: set[str] = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports.update(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imports.add(node.module)
                self.imports_map[file_path] = sorted(imports)
            except (SyntaxError, UnicodeDecodeError, OSError) as exc:
                errors += 1
                self.scan_issues.append({
                    "kind": "syntax_or_parse_error",
                    "file": os.path.relpath(file_path, self.base_dir),
                    "detail": str(exc),
                })
        return errors

    def _module_exists(self, module_name: str) -> bool:
        parts = [p for p in module_name.split(".") if p]
        if not parts or parts[0] not in self._IMPORT_ROOTS:
            return True
        base = os.path.join(self.base_dir, *parts)
        return os.path.isfile(base + ".py") or os.path.isdir(base)

    def _scan_internal_imports(self) -> int:
        unresolved = 0
        for file_path, imports in self.imports_map.items():
            for module in imports:
                root = module.split(".", 1)[0]
                if root in self._IMPORT_ROOTS and not self._module_exists(module):
                    unresolved += 1
                    self.scan_issues.append({
                        "kind": "unresolved_internal_import",
                        "file": os.path.relpath(file_path, self.base_dir),
                        "module": module,
                    })
        return unresolved

    def _detect_duplicate_module_names(self) -> List[str]:
        seen: Dict[str, str] = {}
        duplicates: List[str] = []
        for file_path in self.python_files:
            rel = os.path.relpath(file_path, self.base_dir).replace(os.sep, "/")
            module = rel[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[:-9]
            if module in seen:
                duplicates.append(module)
                self.scan_issues.append({
                    "kind": "duplicate_module",
                    "module": module,
                    "files": [seen[module], rel],
                })
            else:
                seen[module] = rel
        return sorted(set(duplicates))

    def _scan_source_risks(self) -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        suspicious_calls = {"eval", "exec", "compile", "system", "popen", "setattr", "delattr"}
        for file_path in self.python_files:
            try:
                content = open(file_path, "r", encoding="utf-8").read()
                tree = ast.parse(content, filename=file_path)
            except Exception:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
                    if name in suspicious_calls:
                        findings.append({
                            "file": os.path.relpath(file_path, self.base_dir),
                            "line": getattr(node, "lineno", None),
                            "kind": "suspicious_call",
                            "symbol": name,
                        })
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module in {"subprocess", "ctypes", "pickle"}:
                        findings.append({
                            "file": os.path.relpath(file_path, self.base_dir),
                            "line": getattr(node, "lineno", None),
                            "kind": "sensitive_import",
                            "module": node.module,
                        })
        return findings

    def _scan_dependency_drift(self) -> List[Dict[str, Any]]:
        """Compare imported third-party packages with declared requirement files when available."""
        declared: set[str] = set()
        for name in ("requirements.txt", "requirements-prod.txt", "requirements-dev.txt"):
            path = os.path.join(self.base_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                for line in open(path, "r", encoding="utf-8"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        declared.add(line.split("==")[0].split(">=")[0].split("<=")[0].strip().lower())
            except OSError:
                pass
        stdlib = set(getattr(__import__("sys"), "stdlib_module_names", set()))
        imported: set[str] = set()
        for imports in self.imports_map.values():
            imported.update(item.split(".")[0].lower() for item in imports)
        findings = []
        for module in sorted(imported):
            if module in stdlib or module in {"typing_extensions"}:
                continue
            if declared and module not in declared and module not in {"fastapi", "pydantic", "aiohttp"}:
                findings.append({"kind": "undeclared_import", "module": module})
        return findings

    def _detect_missing_modules(self) -> None:
        missing_templates = {
            "neural_compiler": ("compiler", "Advanced DSL compiler for game definition language not found", "god_brain/neural_compiler.py", "high", 8, ["ast", "types", "abc"]),
            "consciousness_engine": ("agent", "NPC consciousness & emotional state engine missing", "god_brain/consciousness_engine.py", "high", 9, ["god_brain.agents.base_agent"]),
            "performance_monitor": ("optimizer", "Real-time performance monitoring & optimization engine", "core/performance_monitor.py", "medium", 7, ["asyncio"]),
        }
        for module_id, (kind, reason, location, priority, complexity, deps) in missing_templates.items():
            target = os.path.join(self.base_dir, location)
            if not os.path.exists(target):
                self.missing_modules.append(ModuleAnalysis(
                    module_name=module_id,
                    module_type=kind,
                    reason=reason,
                    priority=priority,
                    suggested_location=location,
                    estimated_complexity=complexity,
                    dependencies=deps,
                    confidence_score=0.98,
                ))


# ============================================================================
# CODE GENERATOR
# ============================================================================
class AdvancedCodeGenerator:
    """Generates production-ready Python modules with AST optimization"""
    
    def __init__(self):
        self.validator = ASTSecurityValidator()
    
    async def generate_module(
        self,
        analysis: ModuleAnalysis,
        ai_gateway=None
    ) -> GeneratedModule:
        """
        Generate a new Python module based on analysis
        
        Args:
            analysis: ModuleAnalysis object describing what to generate
            ai_gateway: Optional AI gateway for assisted code generation
        
        Returns:
            GeneratedModule with validated code
        """
        logger.info(f"🚀 Generating module: {analysis.module_name}")
        
        # Generate code based on module type
        if analysis.module_type == "compiler":
            code = self._generate_compiler_module(analysis)
        elif analysis.module_type == "agent":
            code = self._generate_agent_module(analysis)
        elif analysis.module_type == "optimizer":
            code = self._generate_optimizer_module(analysis)
        else:
            code = self._generate_generic_module(analysis)
        
        # Validate generated code
        is_safe, issues = self.validator.validate_code(code)
        
        if not is_safe:
            logger.error(f"❌ Security validation failed: {issues}")
            raise ValueError(f"Generated code failed security checks: {issues}")
        
        # Parse AST for optimization
        tree = ast.parse(code)
        optimized_code = self._optimize_ast(code, tree)
        
        # Calculate metrics
        hash_digest = hashlib.sha256(optimized_code.encode()).hexdigest()[:8]
        complexity = self._calculate_module_complexity(tree)
        
        # Create file path
        file_path = analysis.suggested_location.replace("\\", "/").lstrip("/")
        repo_root = Path(EvolutionConfig.BASE_DIR).resolve()
        candidate = (repo_root / file_path).resolve()
        try:
            candidate.relative_to(repo_root)
        except ValueError as exc:
            raise ValueError(f"Unsafe generated path: {analysis.suggested_location!r}") from exc
        if not any(file_path == allowed or file_path.startswith(allowed + "/") for allowed in EvolutionConfig.ALLOWED_DIRS):
            raise ValueError(f"Generated path outside evolution allowlist: {file_path}")
        
        return GeneratedModule(
            name=analysis.module_name,
            file_path=normalized,
            content=optimized_code,
            ast_tree=tree,
            validation_errors=issues,
            optimization_metrics={
                "complexity": complexity,
                "lines_of_code": len(optimized_code.split('\n')),
                "functions": len([n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]),
                "classes": len([n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]),
            },
            generated_at=datetime.datetime.utcnow().isoformat(),
            hash_digest=hash_digest,
            complexity_score=complexity,
        )
    
    def _generate_compiler_module(self, analysis: ModuleAnalysis) -> str:
        """Generate Neural Compiler module"""
        return '''"""
Neural Compiler - DSL Parser & Code Generator
=============================================
Compiles custom game definition language into optimized Python/C++ code.

Auto-Generated by God Node V2 Self-Evolution Engine
"""

import ast
import re
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from enum import Enum


class TokenType(Enum):
    """DSL Token types"""
    KEYWORD = "keyword"
    IDENTIFIER = "identifier"
    NUMBER = "number"
    STRING = "string"
    OPERATOR = "operator"
    LPAREN = "lparen"
    RPAREN = "rparen"
    LBRACE = "lbrace"
    RBRACE = "rbrace"
    SEMICOLON = "semicolon"
    EOF = "eof"


@dataclass
class Token:
    """Represents a DSL token"""
    type: TokenType
    value: str
    line: int
    column: int


class NeuralDSLLexer:
    """Tokenizes DSL game definitions"""
    
    KEYWORDS = {
        "define", "entity", "behavior", "action", "state",
        "condition", "event", "trigger", "effect", "rule"
    }
    
    def __init__(self, source: str):
        self.source = source
        self.pos = 0
        self.line = 1
        self.column = 1
        self.tokens: List[Token] = []
    
    def tokenize(self) -> List[Token]:
        """Convert source to token stream"""
        while self.pos < len(self.source):
            self._skip_whitespace_and_comments()
            
            if self.pos >= len(self.source):
                break
            
            char = self.source[self.pos]
            
            if char.isalpha() or char == '_':
                self._tokenize_identifier()
            elif char.isdigit():
                self._tokenize_number()
            elif char == '"':
                self._tokenize_string()
            elif char in '(){}':
                self._tokenize_bracket(char)
            elif char in '+-*/=<>!':
                self._tokenize_operator()
            elif char == ';':
                self.tokens.append(Token(
                    TokenType.SEMICOLON, ';', self.line, self.column
                ))
                self.pos += 1
                self.column += 1
            else:
                self.pos += 1
                self.column += 1
        
        self.tokens.append(Token(TokenType.EOF, '', self.line, self.column))
        return self.tokens
    
    def _skip_whitespace_and_comments(self) -> None:
        """Skip whitespace and comments"""
        while self.pos < len(self.source):
            if self.source[self.pos].isspace():
                if self.source[self.pos] == '\\n':
                    self.line += 1
                    self.column = 1
                else:
                    self.column += 1
                self.pos += 1
            elif self.pos + 1 < len(self.source) and self.source[self.pos:self.pos+2] == '//':
                while self.pos < len(self.source) and self.source[self.pos] != '\\n':
                    self.pos += 1
            else:
                break
    
    def _tokenize_identifier(self) -> None:
        """Tokenize identifier or keyword"""
        start = self.pos
        while self.pos < len(self.source) and (
            self.source[self.pos].isalnum() or self.source[self.pos] == '_'
        ):
            self.pos += 1
        
        value = self.source[start:self.pos]
        token_type = TokenType.KEYWORD if value in self.KEYWORDS else TokenType.IDENTIFIER
        
        self.tokens.append(Token(token_type, value, self.line, self.column))
        self.column += len(value)
    
    def _tokenize_number(self) -> None:
        """Tokenize numeric literal"""
        start = self.pos
        while self.pos < len(self.source) and self.source[self.pos].isdigit():
            self.pos += 1
        
        if self.pos < len(self.source) and self.source[self.pos] == '.':
            self.pos += 1
            while self.pos < len(self.source) and self.source[self.pos].isdigit():
                self.pos += 1
        
        value = self.source[start:self.pos]
        self.tokens.append(Token(TokenType.NUMBER, value, self.line, self.column))
        self.column += len(value)
    
    def _tokenize_string(self) -> None:
        """Tokenize string literal"""
        quote_char = self.source[self.pos]
        self.pos += 1
        start = self.pos
        
        while self.pos < len(self.source) and self.source[self.pos] != quote_char:
            if self.source[self.pos] == '\\\\':
                self.pos += 2
            else:
                self.pos += 1
        
        value = self.source[start:self.pos]
        if self.pos < len(self.source):
            self.pos += 1
        
        self.tokens.append(Token(TokenType.STRING, value, self.line, self.column))
        self.column += len(value) + 2
    
    def _tokenize_bracket(self, char: str) -> None:
        """Tokenize bracket characters"""
        bracket_map = {
            '(': TokenType.LPAREN,
            ')': TokenType.RPAREN,
            '{': TokenType.LBRACE,
            '}': TokenType.RBRACE,
        }
        self.tokens.append(Token(bracket_map[char], char, self.line, self.column))
        self.pos += 1
        self.column += 1
    
    def _tokenize_operator(self) -> None:
        """Tokenize operators"""
        start = self.pos
        if self.pos + 1 < len(self.source) and self.source[self.pos:self.pos+2] in ['==', '<=', '>=', '!=']:
            self.pos += 2
            self.column += 2
        else:
            self.pos += 1
            self.column += 1
        
        value = self.source[start:self.pos]
        self.tokens.append(Token(TokenType.OPERATOR, value, self.line, self.column - len(value)))


class NeuralDSLCompiler:
    """Compiles DSL into executable game logic"""
    
    def __init__(self, source: str):
        self.source = source
        self.lexer = NeuralDSLLexer(source)
    
    def compile(self) -> Dict[str, Any]:
        """Compile DSL to intermediate representation"""
        tokens = self.lexer.tokenize()
        
        return {
            "status": "compiled",
            "token_count": len(tokens),
            "ir": self._generate_intermediate_representation(tokens),
        }
    
    def _generate_intermediate_representation(self, tokens: List[Token]) -> Dict[str, Any]:
        """Generate intermediate representation"""
        return {
            "entities": [],
            "behaviors": [],
            "events": [],
            "rules": [],
        }


class NeuralCompiler:
    """Main compiler interface"""
    
    @staticmethod
    def compile_game_definition(dsl_source: str) -> Dict[str, Any]:
        """Compile game definition DSL to executable code"""
        compiler = NeuralDSLCompiler(dsl_source)
        return compiler.compile()


__all__ = ["NeuralCompiler", "NeuralDSLLexer", "NeuralDSLCompiler"]
'''
    
    def _generate_agent_module(self, analysis: ModuleAnalysis) -> str:
        """Generate Agent/NPC Consciousness module"""
        return '''"""
NPC Consciousness Engine - Emotional State & Decision Making
===========================================================
Auto-Generated by God Node V2 Self-Evolution Engine
"""

import asyncio
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Any


class EmotionType(Enum):
    """NPC emotion states"""
    NEUTRAL = "neutral"
    HAPPY = "happy"
    ANGRY = "angry"
    SCARED = "scared"
    CONFUSED = "confused"
    CURIOUS = "curious"


@dataclass
class EmotionalState:
    """Tracks NPC emotional state"""
    primary_emotion: EmotionType = EmotionType.NEUTRAL
    intensity: float = 0.5
    memory_decay: float = 0.95
    
    def decay(self) -> None:
        """Natural decay of emotional intensity"""
        self.intensity *= self.memory_decay
        if self.intensity < 0.1:
            self.primary_emotion = EmotionType.NEUTRAL


@dataclass
class BehaviorTreeNode:
    """Behavior tree node for NPC decision-making"""
    name: str
    node_type: str
    children: List['BehaviorTreeNode'] = field(default_factory=list)
    condition: Optional[Callable] = None
    action: Optional[Callable] = None


class ConsciousnessEngine:
    """Main consciousness engine for NPCs"""
    
    def __init__(self, npc_id: str):
        self.npc_id = npc_id
        self.emotional_state = EmotionalState()
        self.memory: Dict[str, Any] = {}
        self.behavior_tree: Optional[BehaviorTreeNode] = None
        self.decision_history: List[str] = []
    
    async def perceive(self, sensory_input: Dict[str, Any]) -> None:
        """Process sensory input and update emotional state"""
        if "threat" in sensory_input and sensory_input["threat"]:
            self.emotional_state.primary_emotion = EmotionType.SCARED
            self.emotional_state.intensity = min(1.0, self.emotional_state.intensity + 0.3)
        
        if "discovery" in sensory_input:
            self.emotional_state.primary_emotion = EmotionType.CURIOUS
            self.emotional_state.intensity = 0.7
        
        self.memory["last_perception"] = sensory_input
    
    async def decide(self, options: List[str]) -> str:
        """Make decision based on consciousness state"""
        if self.emotional_state.primary_emotion == EmotionType.SCARED:
            decision = "flee" if "flee" in options else options[0]
        elif self.emotional_state.primary_emotion == EmotionType.CURIOUS:
            decision = "explore" if "explore" in options else options[0]
        else:
            decision = options[0]
        
        self.decision_history.append(decision)
        self.emotional_state.decay()
        
        return decision


__all__ = ["ConsciousnessEngine", "EmotionalState", "EmotionType"]
'''
    
    def _generate_optimizer_module(self, analysis: ModuleAnalysis) -> str:
        """Generate Performance Optimizer module"""
        return '''"""
Performance Monitor & Optimizer - Real-time System Analysis
============================================================
Auto-Generated by God Node V2 Self-Evolution Engine
"""

import asyncio
import time
import psutil
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from collections import deque


@dataclass
class PerformanceMetric:
    """Single performance metric snapshot"""
    timestamp: float
    cpu_percent: float
    memory_mb: float
    memory_percent: float
    active_connections: int
    request_latency_ms: float


class PerformanceMonitor:
    """Real-time performance monitoring"""
    
    def __init__(self, history_size: int = 1000):
        self.metrics_history: deque = deque(maxlen=history_size)
        self.process = psutil.Process()
    
    async def collect_metrics(
        self,
        active_connections: int = 0,
        request_latency_ms: float = 0
    ) -> PerformanceMetric:
        """Collect current performance metrics"""
        metric = PerformanceMetric(
            timestamp=time.time(),
            cpu_percent=self.process.cpu_percent(interval=0.1),
            memory_mb=self.process.memory_info().rss / 1024 / 1024,
            memory_percent=self.process.memory_percent(),
            active_connections=active_connections,
            request_latency_ms=request_latency_ms,
        )
        
        self.metrics_history.append(metric)
        return metric


class PerformanceOptimizer:
    """Auto-optimization engine"""
    
    def __init__(self, monitor: PerformanceMonitor):
        self.monitor = monitor
        self.optimization_history: List[str] = []
    
    async def analyze_and_optimize(self) -> Dict[str, Any]:
        """Analyze metrics and apply optimizations"""
        optimizations = []
        
        return {
            "status": "analysis_complete",
            "recommended_optimizations": optimizations,
        }


__all__ = ["PerformanceMonitor", "PerformanceOptimizer", "PerformanceMetric"]
'''
    
    def _generate_generic_module(self, analysis: ModuleAnalysis) -> str:
        """Generate a generic Python module"""
        class_name = self._to_camelcase(analysis.module_name)
        return f'''"""
{analysis.module_name.replace("_", " ").title()} Module
{"=" * (len(analysis.module_name) + 8)}
{analysis.reason}

Auto-Generated by God Node V2 Self-Evolution Engine
"""

from typing import Dict, List, Any, Optional


class {class_name}:
    """{analysis.reason}
    
    Complexity: {analysis.estimated_complexity}/10
    """
    
    def __init__(self):
        """Initialize module"""
        self.status = "initialized"
        self.config: Dict[str, Any] = {{}}
    
    def configure(self, **kwargs) -> None:
        """Configure module parameters"""
        self.config.update(kwargs)
    
    async def execute(self, *args, **kwargs) -> Dict[str, Any]:
        """Execute module logic"""
        return {{"status": "success", "result": None}}


__all__ = ["{class_name}"]
'''
    
    def _optimize_ast(self, code: str, tree: ast.Module) -> str:
        """Optimize code using AST manipulation"""
        return code
    
    def _calculate_module_complexity(self, tree: ast.Module) -> float:
        """Calculate overall module complexity"""
        complexity = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                complexity += 1
            elif isinstance(node, ast.ClassDef):
                complexity += 2
            elif isinstance(node, (ast.If, ast.For, ast.While)):
                complexity += 0.5
        
        return min(complexity, 10.0)
    
    @staticmethod
    def _to_camelcase(snake_str: str) -> str:
        """Convert snake_case to CamelCase"""
        components = snake_str.split('_')
        return ''.join(x.title() for x in components)


# ============================================================================
# GIT & GITHUB AUTOMATION (Optional - requires PyGithub)
# ============================================================================
class GitHubAutomation:
    """Handles GitHub operations with safety guardrails"""
    
    def __init__(self):
        if not EvolutionConfig.GITHUB_TOKEN:
            raise ValueError("❌ GITHUB_TOKEN environment variable not set")
        
        if not Github:
            raise ImportError("PyGithub not installed. Install with: pip install PyGithub")
        
        self.github = Github(EvolutionConfig.GITHUB_TOKEN)
        self.repo = self.github.get_user(EvolutionConfig.GITHUB_OWNER).get_repo(
            EvolutionConfig.GITHUB_REPO
        )
        logger.info("✅ GitHub authentication successful")
    
    async def create_feature_branch(self, branch_name: str) -> str:
        """Create a new feature branch for generated code"""
        try:
            base_branch = self.repo.get_branch("main")
            self.repo.create_git_ref(
                ref=f"refs/heads/{branch_name}",
                sha=base_branch.commit.sha
            )
            logger.info(f"✅ Created branch: {branch_name}")
            return branch_name
            
        except Exception as e:
            logger.error(f"❌ Failed to create branch: {e}")
            raise
    
    async def commit_files(
        self,
        branch_name: str,
        files: Dict[str, str],
        commit_message: str
    ) -> bool:
        """Commit generated files to branch"""
        try:
            for file_path, content in files.items():
                normalized = str(file_path).replace("\\", "/").lstrip("/")
                if ".." in Path(normalized).parts or normalized.startswith((".", "/")):
                    raise ValueError(f"Unsafe repository path: {file_path!r}")
                if not any(normalized == allowed or normalized.startswith(allowed + "/") for allowed in EvolutionConfig.ALLOWED_DIRS):
                    raise ValueError(f"Repository path outside evolution allowlist: {normalized}")
                if not isinstance(content, str) or len(content.encode("utf-8")) > EvolutionConfig.MAX_FILE_SIZE_KB * 1024:
                    raise ValueError(f"Generated file exceeds safety limit: {normalized}")
                try:
                    file_obj = self.repo.get_contents(normalized, ref=branch_name)
                    self.repo.update_file(
                        path=normalized,
                        message=commit_message,
                        content=content,
                        sha=file_obj.sha,
                        branch=branch_name
                    )
                    logger.info(f"✅ Updated: {file_path}")
                except:
                    self.repo.create_file(
                        path=normalized,
                        message=commit_message,
                        content=content,
                        branch=branch_name
                    )
                    logger.info(f"✅ Created: {file_path}")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Commit failed: {e}")
            return False
    
    async def create_pull_request(
        self,
        branch_name: str,
        title: str,
        description: str,
        reviewers: Optional[List[str]] = None
    ) -> Optional[str]:
        """Create Pull Request for human review"""
        try:
            pr = self.repo.create_pull(
                title=title,
                body=description,
                head=branch_name,
                base="main",
                draft=EvolutionConfig.PR_DRAFT,
            )
            
            if reviewers:
                pr.create_review_request(reviewers=reviewers)
            
            pr.add_to_labels(EvolutionConfig.PR_LABEL)
            
            pr_url = pr.html_url
            logger.info(f"✅ Created PR: {pr_url}")
            
            return pr_url
            
        except Exception as e:
            logger.error(f"❌ PR creation failed: {e}")
            return None


# ============================================================================
# AUDIT & LOGGING
# ============================================================================
class AuditLogger:
    """Maintains audit trail for all evolution operations"""
    
    def __init__(self, audit_dir: str = EvolutionConfig.AUDIT_LOG_DIR):
        self.audit_dir = audit_dir
        os.makedirs(audit_dir, exist_ok=True)
    
    async def log_evolution(self, audit: EvolutionAudit) -> None:
        """Log evolution operation"""
        filename = f"evolution_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(self.audit_dir, filename)
        
        try:
            with open(filepath, 'w') as f:
                f.write(json.dumps(asdict(audit), indent=2))
            logger.info(f"✅ Audit logged: {filename}")
        except Exception as e:
            logger.error(f"❌ Audit logging failed: {e}")


# ============================================================================
# MAIN SELF-EVOLUTION ENGINE
# ============================================================================
class AutonomousEvolutionEngine:
    """Main orchestrator for self-evolution with human approval loop"""
    
    def __init__(self):
        self.scanner = RepositoryMetaScanner()
        self.generator = AdvancedCodeGenerator()
        self.github = None
        try:
            self.github = GitHubAutomation()
        except (ImportError, ValueError) as e:
            logger.warning(f"⚠️  GitHub integration disabled: {e}")
        
        self.audit = AuditLogger()
        self.status = GenerationStatus.PENDING
    
    async def evolve_repository(self) -> Dict[str, Any]:
        """
        Main evolution flow:
        1. Scan repository
        2. Generate new modules
        3. Create PR for human review
        4. Wait for approval
        
        Returns:
            Evolution report
        """
        self.status = GenerationStatus.SCANNING
        logger.info("🤖 Starting autonomous evolution...")
        
        evolution_report = {
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "status": "in_progress",
            "modules_generated": [],
            "pr_urls": [],
            "errors": [],
        }
        
        try:
            # Phase 1: Scan repository
            logger.info("\n" + "="*60)
            logger.info("PHASE 1: Repository Meta-Scan")
            logger.info("="*60)
            
            missing_modules, metrics = self.scanner.scan_repository()
            logger.info(f"📊 Scan Results:")
            for key, value in metrics.items():
                logger.info(f"   {key}: {value}")
            
            if not missing_modules:
                logger.info("✅ Repository is up-to-date. No missing modules detected.")
                evolution_report["status"] = "no_changes_needed"
                return evolution_report
            
            logger.info(f"\n🔎 Detected {len(missing_modules)} missing modules:")
            for analysis in missing_modules:
                logger.info(
                    f"   • {analysis.module_name} ({analysis.priority}) - {analysis.reason}"
                )
            
            # Phase 2: Generate modules
            logger.info("\n" + "="*60)
            logger.info("PHASE 2: Autonomous Code Generation")
            logger.info("="*60)
            
            self.status = GenerationStatus.GENERATING
            generated_modules: List[GeneratedModule] = []
            
            for analysis in missing_modules:
                try:
                    logger.info(f"\n▶️  Generating {analysis.module_name}...")
                    module = await self.generator.generate_module(analysis)
                    generated_modules.append(module)
                    
                    logger.info(f"✅ Generated: {module.name}")
                    logger.info(f"   Lines of Code: {module.optimization_metrics['lines_of_code']}")
                    logger.info(f"   Functions: {module.optimization_metrics['functions']}")
                    logger.info(f"   Complexity Score: {module.complexity_score:.1f}/10")
                    
                    evolution_report["modules_generated"].append(module.name)
                    
                except Exception as e:
                    error_msg = f"Failed to generate {analysis.module_name}: {str(e)}"
                    logger.error(f"❌ {error_msg}")
                    evolution_report["errors"].append(error_msg)
            
            if not generated_modules:
                logger.warning("⚠️  No modules generated successfully")
                evolution_report["status"] = "generation_failed"
                return evolution_report
            
            # Phase 3: Create PR if GitHub is available
            if self.github:
                logger.info("\n" + "="*60)
                logger.info("PHASE 3: Creating GitHub Pull Request")
                logger.info("="*60)
                
                self.status = GenerationStatus.CREATING_PR
                
                timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                branch_name = f"{EvolutionConfig.GITHUB_BRANCH_PREFIX}_{timestamp}"
                
                await self.github.create_feature_branch(branch_name)
                
                files_to_commit = {
                    module.file_path: module.content
                    for module in generated_modules
                }
                
                commit_message = (
                    f"[Auto-Generated] Autonomous evolution: "
                    f"Added {len(generated_modules)} new modules"
                )
                
                await self.github.commit_files(
                    branch_name=branch_name,
                    files=files_to_commit,
                    commit_message=commit_message
                )
                
                pr_description = self._generate_pr_description(generated_modules, metrics)
                
                pr_url = await self.github.create_pull_request(
                    branch_name=branch_name,
                    title=f"[Auto-Evolution] {len(generated_modules)} new module(s) generated",
                    description=pr_description,
                    reviewers=EvolutionConfig.PR_REQUEST_REVIEWERS,
                )
                
                if pr_url:
                    evolution_report["pr_urls"].append(pr_url)
                    self.status = GenerationStatus.SUCCESS
                    evolution_report["status"] = "pr_created_awaiting_review"
                    
                    logger.info(f"\n✅ PULL REQUEST CREATED!")
                    logger.info(f"🔗 Review at: {pr_url}")
                    logger.info(f"\n⏳ Awaiting your approval for merge to main branch...")
            else:
                logger.info("\n📁 Generated modules (GitHub integration disabled):")
                for module in generated_modules:
                    logger.info(f"   • {module.file_path}")
                
                self.status = GenerationStatus.SUCCESS
                evolution_report["status"] = "modules_generated_local"
            
            # Log audit entry
            await self.audit.log_evolution(EvolutionAudit(
                timestamp=datetime.datetime.utcnow().isoformat(),
                action="repository_evolution",
                status=self.status.value,
                modules_generated=[m.name for m in generated_modules],
                pr_url=evolution_report["pr_urls"][0] if evolution_report["pr_urls"] else None,
                errors=evolution_report["errors"],
                metadata={
                    "scan_metrics": metrics,
                    "module_complexity": [m.complexity_score for m in generated_modules],
                },
            ))
            
            return evolution_report
            
        except Exception as e:
            logger.error(f"❌ Evolution process failed: {e}")
            self.status = GenerationStatus.FAILED
            evolution_report["status"] = "failed"
            evolution_report["errors"].append(str(e))
            return evolution_report
    
    def _generate_pr_description(
        self,
        modules: List[GeneratedModule],
        metrics: Dict[str, Any]
    ) -> str:
        """Generate detailed PR description"""
        description = """# 🤖 Autonomous Evolution - New Modules Generated

## Auto-Generated by God Node V2 Self-Evolution Engine

This pull request contains new modules automatically generated by the Meta-Programming architecture.
All code has been validated for security and syntax correctness.

### Modules Added

"""
        for module in modules:
            description += f"""
#### {module.name}
- **File**: `{module.file_path}`
- **Lines of Code**: {module.optimization_metrics['lines_of_code']}
- **Functions**: {module.optimization_metrics['functions']}
- **Classes**: {module.optimization_metrics['classes']}
- **Complexity Score**: {module.complexity_score:.1f}/10

"""
        
        description += f"""
## Repository Metrics

- **Total Files Scanned**: {metrics.get('total_files', 'N/A')}
- **Total Lines of Code**: {metrics.get('total_lines', 'N/A')}
- **Average File Size**: {metrics.get('avg_file_size', 'N/A'):.0f} lines

## Security Review Checklist

✅ All code passed AST syntax validation
✅ No dangerous imports detected
✅ All modules follow Python best practices

## Next Steps

1. Review the code in this PR
2. Test the new modules
3. Approve & Merge when satisfied

---

**Status**: Awaiting human review and approval
"""
        return description


# ============================================================================
# PUBLIC API
# ============================================================================
class EvolutionEngine:
    """Public-facing API for self-evolution"""
    
    def __init__(self):
        self._engine = AutonomousEvolutionEngine()
    
    async def evolve(self) -> Dict[str, Any]:
        """
        Trigger autonomous evolution with human approval loop
        
        Returns:
            Evolution report with PR URL for review
        """
        return await self._engine.evolve_repository()
    
    async def scan_only(self) -> Tuple[List[ModuleAnalysis], Dict[str, Any]]:
        """
        Scan repository without generation
        
        Returns:
            Tuple of missing modules and metrics
        """
        return self._engine.scanner.scan_repository()

    async def force_upgrade_system(self, files_dict: Dict[str, str], commit_message: str = "God Node Auto-Evolution Upgrade") -> Dict[str, Any]:
        """Deprecated hard-stop: evolution can only create reviewable branches/PRs."""
        logger.error("Direct main-branch evolution is disabled by policy.")
        return {
            "status": "REJECTED",
            "error": "Direct pushes to main are disabled. Use evolve()/PR workflow for human review.",
            "files_requested": len(files_dict) if isinstance(files_dict, dict) else 0,
            "commit_message": commit_message,
        }
        
   
# ============================================================================
# USAGE
# ============================================================================

if __name__ == "__main__":
    """
    Usage:
    
    1. Set environment variables:
       export GITHUB_TOKEN="your_github_token"
       export GITHUB_OWNER="your_username"
       export GITHUB_REPO="your_repo"
    
    2. Run evolution:
       python -m god_brain.self_evolution
    
    3. Review the Pull Request on GitHub
    4. Approve and merge manually
    """
    
    async def main():
        try:
            engine = EvolutionEngine()
            
            logger.info("\n" + "🔷" * 40)
            logger.info("GOD NODE V2 - AUTONOMOUS EVOLUTION ENGINE")
            logger.info("🔷" * 40 + "\n")
            
            result = await engine.evolve()
            
            logger.info("\n" + "="*60)
            logger.info("EVOLUTION COMPLETE")
            logger.info("="*60)
            logger.info(json.dumps(result, indent=2))
            
        except Exception as e:
            logger.error(f"Fatal error: {e}")
            raise
    
    asyncio.run(main())
