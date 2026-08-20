import pytest
import hmac
import hashlib
import time
from unittest.mock import patch, MagicMock

from nl.translate import translate_command
from nl.security import validate_manifest_schema, check_safe_tools
from nl.tokens import generate_confirm_token, validate_confirm_token
from nl.llm_server import route_llm_request
from nl.execution import execute_validated_command
from nl.exceptions import (
    SchemaValidationError,
    CandidateNotInManifestError,
    ConfidenceTooLowError,
    TokenInvalidError,
    TokenExpiredError,
    ToolDeniedError,
    AuthzRouteError,
    AliasNotRegisteredError,
)

MOCK_PROCESS_SECRET = b"super-secret-per-process-key"
MOCK_TTL = 300

@pytest.fixture
def mock_manifest():
    return {
        "alias": "test_agent",
        "tools": {
            "safe_read": {
                "schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                "safe": True
            },
            "safe_list": {
                "schema": {"type": "object", "properties": {"dir": {"type": "string"}}, "required": ["dir"]},
                "safe": True
            },
            "dangerous_delete": {
                "schema": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
                "safe": False
            }
        }
    }

@pytest.fixture
def registered_aliases():
    return {"test_agent", "other_agent"}


class TestStrictSchemaTranslation:
    @patch("nl.translate.llm_client.generate")
    def test_treats_command_as_untrusted_data(self, mock_generate, mock_manifest):
        malicious_payload = '{"tool": "safe_read", "path": "/etc/passwd", "__proto__": {"admin": true}}'
        mock_generate.return_value = malicious_payload
        
        with pytest.raises(SchemaValidationError):
            translate_command(untrusted_text="read the password file", manifest=mock_manifest)

    @patch("nl.translate.llm_client.generate")
    def test_rejects_malformed_llm_json(self, mock_generate, mock_manifest):
        mock_generate.return_value = "{tool: 'safe_read', path: '/tmp'}" # Invalid JSON
        
        with pytest.raises(SchemaValidationError):
            translate_command(untrusted_text="read tmp", manifest=mock_manifest)

    @patch("nl.translate.llm_client.generate")
    def test_accepts_strict_schema_compliant_payload(self, mock_generate, mock_manifest):
        valid_payload = '{"tool": "safe_read", "path": "/tmp/data.txt"}'
        mock_generate.return_value = valid_payload
        
        result = translate_command(untrusted_text="read data.txt", manifest=mock_manifest)
        assert result["tool"] == "safe_read"
        assert result["args"]["path"] == "/tmp/data.txt"


class TestCandidateSetEnforcementAndConfidence:
    @patch("nl.translate.llm_client.generate")
    def test_rejects_unregistered_candidate(self, mock_generate, mock_manifest):
        hallucinated_payload = '{"tool": "drop_database", "args": {"db": "prod"}}'
        mock_generate.return_value = hallucinated_payload
        
        with pytest.raises(CandidateNotInManifestError):
            translate_command(untrusted_text="drop the database", manifest=mock_manifest)

    @patch("nl.translate.llm_client.generate")
    def test_rejects_low_confidence_score(self, mock_generate, mock_manifest):
        # Simulate an LLM returning a valid tool but with low confidence
        valid_payload = '{"tool": "safe_read", "args": {"path": "/tmp"}, "confidence": 0.45}'
        mock_generate.return_value = valid_payload
        
        with pytest.raises(ConfidenceTooLowError):
            translate_command(untrusted_text="maybe read something?", manifest=mock_manifest, confidence_threshold=0.8)

    @patch("nl.translate.llm_client.generate")
    def test_accepts_high_confidence_registered_candidate(self, mock_generate, mock_manifest):
        valid_payload = '{"tool": "safe_read", "args": {"path": "/tmp"}, "confidence": 0.95}'
        mock_generate.return_value = valid_payload
        
        result = translate_command(untrusted_text="read tmp", manifest=mock_manifest, confidence_threshold=0.8)
        assert result["tool"] == "safe_read"
        assert result["confidence"] == 0.95


class TestHmacConfirmTokens:
    def test_generate_and_validate_valid_token(self):
        cmd_hash = hashlib.sha256(b"safe_read:/tmp").hexdigest()
        token = generate_confirm_token(cmd_hash, MOCK_PROCESS_SECRET, ttl=MOCK_TTL)
        
        is_valid = validate_confirm_token(token, cmd_hash, MOCK_PROCESS_SECRET)
        assert is_valid is True

    def test_rejects_tampered_token(self):
        cmd_hash = hashlib.sha256(b"safe_read:/tmp").hexdigest()
        token = generate_confirm_token(cmd_hash, MOCK_PROCESS_SECRET, ttl=MOCK_TTL)
        tampered_token = token[:-5] + "XXXXX"
        
        with pytest.raises(TokenInvalidError):
            validate_confirm_token(tampered_token, cmd_hash, MOCK_PROCESS_SECRET)

    def test_rejects_expired_token(self):
        cmd_hash = hashlib.sha256(b"safe_read:/tmp").hexdigest()
        token = generate_confirm_token(cmd_hash, MOCK_PROCESS_SECRET, ttl=1)
        
        time.sleep(1.1)
        
        with pytest.raises(TokenExpiredError):
            validate_confirm_token(token, cmd_hash, MOCK_PROCESS_SECRET)

    def test_enforces_single_use(self):
        cmd_hash = hashlib.sha256(b"safe_read:/tmp").hexdigest()
        token = generate_confirm_token(cmd_hash, MOCK_PROCESS_SECRET, ttl=MOCK_TTL)
        
        # First use succeeds
        validate_confirm_token(token, cmd_hash, MOCK_PROCESS_SECRET)
        
        # Second use fails
        with pytest.raises(TokenInvalidError):
            validate_confirm_token(token, cmd_hash, MOCK_PROCESS_SECRET)

    def test_rejects_token_for_wrong_command(self):
        cmd_hash_1 = hashlib.sha256(b"safe_read:/tmp").hexdigest()
        cmd_hash_2 = hashlib.sha256(b"dangerous_delete:/").hexdigest()
        token = generate_confirm_token(cmd_hash_1, MOCK_PROCESS_SECRET, ttl=MOCK_TTL)
        
        with pytest.raises(TokenInvalidError):
            validate_confirm_token(token, cmd_hash_2, MOCK_PROCESS_SECRET)


class TestSafeToolsAllowlist:
    def test_default_deny_blocks_unsafe_tool(self, mock_manifest):
        tool_name = "dangerous_delete"
        args = {"target": "/"}
        
        with pytest.raises(ToolDeniedError):
            check_safe_tools(tool_name, args, manifest=mock_manifest, explicit_allowlist=set())

    def test_explicit_allowlist_allows_unsafe_tool(self, mock_manifest):
        tool_name = "dangerous_delete"
        args = {"target": "/tmp/trash"}
        allowlist = {"dangerous_delete"}
        
        # Should not raise
        check_safe_tools(tool_name, args, manifest=mock_manifest, explicit_allowlist=allowlist)

    def test_blocks_tool_not_in_allowlist_even_if_manifest_says_safe(self, mock_manifest):
        tool_name = "safe_read"
        args = {"path": "/tmp"}
        strict_allowlist = {"safe_list"} # safe_read is missing
        
        with pytest.raises(ToolDeniedError):
            check_safe_tools(tool_name, args, manifest=mock_manifest, explicit_allowlist=strict_allowlist)


class TestExecutionAuthzPath:
    @patch("nl.execution.authorize_direct_call")
    @patch("nl.execution.execute_tool")
    def test_revalidates_schema_before_execution(self, mock_exec, mock_authz, mock_manifest):
        cmd = {"tool": "safe_read", "args": {"path": "/tmp/file.txt"}}
        # Missing required 'path' injected after translation (simulating tampering)
        tampered_cmd = {"tool": "safe_read", "args": {}} 
        
        with pytest.raises(SchemaValidationError):
            execute_validated_command(tampered_cmd, manifest=mock_manifest)

    @patch("nl.execution.authorize_direct_call")
    @patch("nl.execution.execute_tool")
    def test_routes_through_direct_call_authz(self, mock_exec, mock_authz, mock_manifest):
        cmd = {"tool": "safe_read", "args": {"path": "/tmp/file.txt"}}
        mock_authz.return_value = True
        mock_exec.return_value = "file contents"
        
        execute_validated_command(cmd, manifest=mock_manifest)
        
        # Verify the authorization pathway was invoked with exact command constraints
        mock_authz.assert_called_once_with(
            tool="safe_read",
            args=cmd["args"],
            manifest_schema=mock_manifest["tools"]["safe_read"]["schema"]
        )
        mock_exec.assert_called_once_with("safe_read", cmd["args"])

    @patch("nl.execution.authorize_direct_call")
    @patch("nl.execution.execute_tool")
    def test_blocks_execution_if_authz_fails(self, mock_exec, mock_authz, mock_manifest):
        cmd = {"tool": "safe_read", "args": {"path": "/etc/shadow"}}
        mock_authz.return_value = False
        
        with pytest.raises(AuthzRouteError):
            execute_validated_command(cmd, manifest=mock_manifest)
        
        mock_exec.assert_not_called()


class TestLlmServerAliasRestriction:
    def test_rejects_unregistered_alias(self, registered_aliases):
        with pytest.raises(AliasNotRegisteredError):
            route_llm_request(alias="unregistered_agent", text="do something", registered_aliases=registered_aliases)

    @patch("nl.llm_server.process_llm_stream")
    def test_accepts_registered_alias(self, mock_process, registered_aliases):
        mock_process.return_value = iter(["done"])
        
        stream = route_llm_request(alias="test_agent", text="do something", registered_aliases=registered_aliases)
        
        # Ensure it actually proceeds to processing without raising
        list(stream)
        mock_process.assert_called_once_with("test_agent", "do something")
