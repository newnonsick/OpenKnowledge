"""Adversarial stress test suite for M1 Local Storage Adapter and Configuration System.

Covers:
1. Path Traversal & Injection Attacks (nested ../, absolute paths, null bytes, ADS, reserved Windows names, URL encoding, symlinks)
2. High-Concurrency Stress & Atomic Write Guarantees (concurrent overwrites, reader/writer interleaving, zero corrupt reads, .tmp cleanup)
3. Workspace Isolation Stress Tests (cross-workspace collision, workspace deletion isolation, boundary containment)
4. Configuration Edge Cases & Extreme Limits (malformed strings, invalid types, extreme numeric limits, decoupling)
"""

import asyncio
import io
import os
from pathlib import Path
import random
import shutil
import string
import sys
from typing import List
import pytest
from pydantic import ValidationError

from src.gateway.config import (
    AppSettings,
    DatabaseSettings,
    EmbeddingSettings,
    GatewaySettings,
    LLMSettings,
    Settings,
    get_settings,
)
from src.gateway.domain.exceptions import ItemNotFoundException, StorageException
from src.gateway.infrastructure.storage.local_storage import LocalStorageAdapter


# ============================================================================
# 1. ADVERSARIAL PATH TRAVERSAL & INJECTION ATTACKS
# ============================================================================

@pytest.mark.tier5
@pytest.mark.feature("local_storage_path_traversal")
class TestPathTraversalAndInjection:
    """Stress testing path traversal prevention and character sanitization."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "malicious_ws",
        [
            "../",
            "../../..",
            "../../../../../../../../../../windows/system32",
            "....//....//....//etc/passwd",
            "workspace/../other_workspace",
            "workspace/subdir",
            "workspace\\subdir",
            "../workspace",
            ".",
            "..",
            "/etc/shadow",
            "C:\\Windows\\System32",
            "D:\\secret",
            "\\\\127.0.0.1\\c$\\secret",
            "ws\x00",
            "ws\n",
            "ws\r\n",
            "ws*name",
            "ws:name",
            "ws|pipe",
            "ws?query",
            "ws<tag>",
            "ws>redirect",
            'ws"quote',
            "   ",
            "",
            "ws name with spaces",
            "ws.dot",
            "ws/subdir/../",
        ],
    )
    async def test_adversarial_workspace_ids_blocked(
        self, local_storage: LocalStorageAdapter, malicious_ws: str
    ):
        """Verify all malicious workspace ID payloads are strictly rejected with StorageException."""
        with pytest.raises(StorageException):
            await local_storage.save_file(malicious_ws, "valid_file_id", "test.txt", b"payload")

        with pytest.raises(StorageException):
            await local_storage.read_file(malicious_ws, "valid_file_id", "test.txt")

        with pytest.raises(StorageException):
            await local_storage.delete_file(malicious_ws, "valid_file_id", "test.txt")

        with pytest.raises(StorageException):
            await local_storage.delete_workspace(malicious_ws)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "malicious_fid",
        [
            "../evil_id",
            "../../root",
            "file\x00_id",
            "file/id",
            "file\\id",
            "file:id",
            "file*id",
            "file?id",
            "file<id>",
            "file>id",
            'file"id',
            "file|id",
            "",
            "   ",
            "file id spaces",
            "file.id.dots",
            "../../../etc/passwd",
        ],
    )
    async def test_adversarial_file_ids_blocked(
        self, local_storage: LocalStorageAdapter, malicious_fid: str
    ):
        """Verify all malicious file ID payloads are strictly rejected with StorageException."""
        with pytest.raises(StorageException):
            await local_storage.save_file("valid_ws", malicious_fid, "test.txt", b"payload")

        with pytest.raises(StorageException):
            await local_storage.read_file("valid_ws", malicious_fid, "test.txt")

        with pytest.raises(StorageException):
            await local_storage.delete_file("valid_ws", malicious_fid, "test.txt")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "malicious_filename,expected_clean_substr",
        [
            ("../../../../../../../../../../windows/win.ini", "win.ini"),
            ("/etc/passwd", "passwd"),
            ("C:\\Windows\\System32\\drivers\\etc\\hosts", "hosts"),
            ("..\\..\\..\\boot.ini", "boot.ini"),
            ("....//....//....//secret.txt", "secret.txt"),
            ("evil.txt\x00.png", "evil.txt.png"),
            ("\x00/../../etc/shadow", "shadow"),
            ("test\x00\x00.dat", "test.dat"),
            ("%2e%2e%2f%2e%2e%2fpasswd", "%2e%2e%2f%2e%2e%2fpasswd"),
            ("legit.txt:evil.exe", "legit.txt_evil.exe"),
            ("file.txt::$DATA", "file.txt__$DATA"),
            ("file.txt:hidden", "file.txt_hidden"),
            ("my<bad>file:name*is?evil|quote\".pdf", "my_bad_file_name_is_evil_quote_.pdf"),
            ("CON", "CON"),
            ("PRN", "PRN"),
            ("AUX", "AUX"),
            ("NUL", "NUL"),
            ("COM1", "COM1"),
            ("COM9", "COM9"),
            ("LPT1", "LPT1"),
            ("CON.txt", "CON.txt"),
            ("aux.json", "aux.json"),
            ("NUL.dat", "NUL.dat"),
            ("   ", "unnamed_file"),
            ("", "unnamed_file"),
            (".", "unnamed_file"),
            ("..", "unnamed_file"),
            ("...", "unnamed_file"),
            (" \t\r\n. ", "unnamed_file"),
        ],
    )
    async def test_adversarial_filename_sanitization_and_containment(
        self,
        local_storage: LocalStorageAdapter,
        temp_storage_dir: Path,
        malicious_filename: str,
        expected_clean_substr: str,
    ):
        """Verify malicious filenames are safely sanitized and strictly contained in the workspace."""
        ws_id = "sandbox_ws"
        file_id = "f123"
        payload = b"Adversarial payload content"

        saved_path = await local_storage.save_file(ws_id, file_id, malicious_filename, payload)

        # 1. Absolute containment verification
        ws_dir = (temp_storage_dir / ws_id).resolve()
        assert saved_path.exists()
        assert saved_path.is_relative_to(ws_dir), f"{saved_path} escaped {ws_dir}"

        # 2. Sanitization verification
        assert "\x00" not in saved_path.name
        assert ":" not in saved_path.name
        assert "<" not in saved_path.name
        assert ">" not in saved_path.name
        assert "*" not in saved_path.name
        assert "?" not in saved_path.name
        assert "|" not in saved_path.name
        assert '"' not in saved_path.name

        # 3. Read back verification
        read_data = await local_storage.read_file(ws_id, file_id, malicious_filename)
        assert read_data == payload

        # 4. Exists and Delete verification
        assert await local_storage.exists(ws_id, file_id, malicious_filename)
        deleted = await local_storage.delete_file(ws_id, file_id, malicious_filename)
        assert deleted is True
        assert not await local_storage.exists(ws_id, file_id, malicious_filename)

    @pytest.mark.asyncio
    async def test_extremely_long_filename_handling(
        self, local_storage: LocalStorageAdapter, temp_storage_dir: Path
    ):
        """Test handling of extremely long filenames."""
        ws_id = "ws_long_name"
        file_id = "f_long"
        long_filename = "a" * 200 + ".txt"
        payload = b"Long filename test payload"

        saved_path = await local_storage.save_file(ws_id, file_id, long_filename, payload)
        assert saved_path.exists()
        assert saved_path.is_relative_to(temp_storage_dir / ws_id)
        assert await local_storage.read_file(ws_id, file_id, long_filename) == payload


# ============================================================================
# 2. HIGH-CONCURRENCY & ATOMIC WRITE GUARANTEES
# ============================================================================

@pytest.mark.tier5
@pytest.mark.feature("local_storage_concurrency_atomicity")
class TestConcurrencyAndAtomicity:
    """Stress testing atomic writes, concurrent access, and zero corrupt reads."""

    @pytest.mark.asyncio
    async def test_high_concurrency_atomic_overwrites_same_file(
        self, local_storage: LocalStorageAdapter, temp_storage_dir: Path
    ):
        """Simulate 50 concurrent tasks overwriting the EXACT SAME file simultaneously.

        Verifies:
        - No unhandled race conditions or file lock crashes.
        - The resulting file is 100% integral and matches one of the written versions.
        - Zero leftover .tmp files.
        """
        ws_id = "ws_concurrency_overwrites"
        file_id = "target_file_id"
        filename = "shared_state.bin"
        concurrency = 50

        # Each payload has a unique marker and is 10 KB to test atomic chunk writes
        payloads = [f"PAYLOAD_{i:04d}_".encode("utf-8") + (b"X" * 10240) for i in range(concurrency)]

        async def write_worker(idx: int):
            # Introduce slight random jitter
            await asyncio.sleep(random.uniform(0.001, 0.02))
            return await local_storage.save_file(ws_id, file_id, filename, payloads[idx])

        # Run 50 concurrent writes
        results = await asyncio.gather(*[write_worker(i) for i in range(concurrency)])
        assert len(results) == concurrency

        # Read back final file
        final_content = await local_storage.read_file(ws_id, file_id, filename)
        assert len(final_content) == len(payloads[0])
        assert final_content in payloads, "Final content was corrupted or partially written!"

        # Verify no orphan .tmp files remain in workspace directory
        ws_dir = temp_storage_dir / ws_id
        tmp_files = list(ws_dir.glob(".tmp_*"))
        assert len(tmp_files) == 0, f"Found orphan temp files: {tmp_files}"

    @pytest.mark.asyncio
    async def test_high_concurrency_interleaved_readers_and_writers(
        self, local_storage: LocalStorageAdapter
    ):
        """Simulate 40 concurrent readers and 40 concurrent writers operating simultaneously.

        Verifies:
        - Readers NEVER read partial, empty, or corrupted payloads.
        - All read operations return a valid, whole payload from one of the writer iterations.
        """
        ws_id = "ws_interleaved_rw"
        file_id = "rw_file_id"
        filename = "stream_data.bin"

        # Initialize file first
        initial_payload = b"VERSION_0000_" + (b"A" * 8192)
        await local_storage.save_file(ws_id, file_id, filename, initial_payload)

        num_writers = 40
        num_readers = 60
        all_valid_payloads = {initial_payload}

        for i in range(1, num_writers + 1):
            all_valid_payloads.add(f"VERSION_{i:04d}_".encode("utf-8") + (b"B" * 8192))

        read_results: List[bytes] = []
        read_errors: List[Exception] = []

        async def writer(idx: int):
            payload = f"VERSION_{idx:04d}_".encode("utf-8") + (b"B" * 8192)
            await asyncio.sleep(random.uniform(0.001, 0.03))
            await local_storage.save_file(ws_id, file_id, filename, payload)

        async def reader(idx: int):
            await asyncio.sleep(random.uniform(0.001, 0.03))
            try:
                data = await local_storage.read_file(ws_id, file_id, filename)
                read_results.append(data)
            except Exception as e:
                read_errors.append(e)

        # Interleave readers and writers
        tasks = [writer(i) for i in range(1, num_writers + 1)] + [reader(i) for i in range(num_readers)]
        random.shuffle(tasks)
        await asyncio.gather(*tasks)

        assert len(read_errors) == 0, f"Reader encountered exceptions: {read_errors}"
        assert len(read_results) == num_readers

        # Verify zero dirty/partial reads
        for read_bytes in read_results:
            assert len(read_bytes) == 8192 + 13, f"Corrupted read size: {len(read_bytes)}"
            assert read_bytes in all_valid_payloads, "Read invalid or torn payload!"

    @pytest.mark.asyncio
    async def test_high_concurrency_multi_file_parallel_ingestion(
        self, local_storage: LocalStorageAdapter
    ):
        """Simulate 100 parallel file saves across 10 distinct workspaces."""
        workspaces = [f"ws_batch_{w}" for w in range(10)]
        num_files_per_ws = 10
        total_files = len(workspaces) * num_files_per_ws

        expected_records = {}

        async def ingest_task(ws: str, idx: int):
            fid = f"file_{idx:03d}"
            fname = f"doc_{idx:03d}.txt"
            content = f"Payload for workspace {ws} file {idx} with random nonce {os.urandom(16).hex()}".encode()
            expected_records[(ws, fid, fname)] = content
            await local_storage.save_file(ws, fid, fname, content)

        tasks = [
            ingest_task(ws, f_idx)
            for ws in workspaces
            for f_idx in range(num_files_per_ws)
        ]
        await asyncio.gather(*tasks)

        # Verify all total_files were saved cleanly and are readable
        for (ws, fid, fname), expected_content in expected_records.items():
            assert await local_storage.exists(ws, fid, fname)
            actual_content = await local_storage.read_file(ws, fid, fname)
            assert actual_content == expected_content

    @pytest.mark.asyncio
    async def test_concurrency_stream_and_bytes_intermixed(
        self, local_storage: LocalStorageAdapter
    ):
        """Verify concurrent writes using both io.BytesIO stream and raw bytes."""
        ws_id = "ws_stream_mix"
        file_id = "stream_mix_id"
        filename = "mixed.bin"

        stream_payload = b"STREAM_CONTENT_" + (b"S" * 16384)
        bytes_payload = b"BYTES_CONTENT__" + (b"B" * 16384)

        async def write_stream():
            stream = io.BytesIO(stream_payload)
            await local_storage.save_file(ws_id, file_id, filename, stream)

        async def write_bytes():
            await local_storage.save_file(ws_id, file_id, filename, bytes_payload)

        # Gather both
        await asyncio.gather(write_stream(), write_bytes(), write_stream(), write_bytes())

        final = await local_storage.read_file(ws_id, file_id, filename)
        assert final in (stream_payload, bytes_payload)


# ============================================================================
# 3. WORKSPACE ISOLATION STRESS TESTS
# ============================================================================

@pytest.mark.tier5
@pytest.mark.feature("workspace_isolation")
class TestWorkspaceIsolationStress:
    """Stress testing absolute boundary separation and isolation between workspaces."""

    @pytest.mark.asyncio
    async def test_workspace_boundary_isolation_with_identical_file_identifiers(
        self, local_storage: LocalStorageAdapter, temp_storage_dir: Path
    ):
        """Create 20 workspaces with identical file_id and filename and verify complete isolation."""
        num_workspaces = 20
        file_id = "shared_id_001"
        filename = "workspace_manifest.json"

        # Save unique payload to each workspace
        for i in range(num_workspaces):
            ws_id = f"workspace_{i:02d}"
            payload = f'{{"workspace": "{ws_id}", "secret": "token_{i}_{os.urandom(8).hex()}"}}'.encode()
            await local_storage.save_file(ws_id, file_id, filename, payload)

        # Verify each workspace returns strictly its own payload
        for i in range(num_workspaces):
            ws_id = f"workspace_{i:02d}"
            read_bytes = await local_storage.read_file(ws_id, file_id, filename)
            assert f'"workspace": "{ws_id}"'.encode() in read_bytes

        # Verify physical directory structure
        for i in range(num_workspaces):
            ws_id = f"workspace_{i:02d}"
            ws_path = temp_storage_dir / ws_id
            assert ws_path.exists()
            assert ws_path.is_dir()
            assert (ws_path / f"{file_id}_{filename}").exists()

    @pytest.mark.asyncio
    async def test_workspace_deletion_cascade_isolation(
        self, local_storage: LocalStorageAdapter, temp_storage_dir: Path
    ):
        """Verify deleting one workspace leaves all other sibling workspaces 100% intact."""
        ws_keep_1 = "ws_active_alpha"
        ws_delete = "ws_to_destroy"
        ws_keep_2 = "ws_active_beta"

        await local_storage.save_file(ws_keep_1, "f1", "alpha.txt", b"Alpha Data")
        await local_storage.save_file(ws_delete, "f1", "delete.txt", b"Delete Data")
        await local_storage.save_file(ws_keep_2, "f1", "beta.txt", b"Beta Data")

        # Delete ws_delete
        assert await local_storage.delete_workspace(ws_delete) is True
        assert not (temp_storage_dir / ws_delete).exists()

        # Reading from deleted workspace raises ItemNotFoundException
        with pytest.raises(ItemNotFoundException):
            await local_storage.read_file(ws_delete, "f1", "delete.txt")

        # Siblings are intact
        assert await local_storage.read_file(ws_keep_1, "f1", "alpha.txt") == b"Alpha Data"
        assert await local_storage.read_file(ws_keep_2, "f1", "beta.txt") == b"Beta Data"


# ============================================================================
# 4. CONFIGURATION EDGE CASES & EXTREME LIMITS
# ============================================================================

@pytest.mark.tier5
@pytest.mark.feature("config_validation_stress")
class TestConfigurationStress:
    """Stress testing Pydantic Settings under extreme limits, malformed envs, and invalid types."""

    def test_api_keys_malformed_and_extreme_formats(self):
        """Test API keys parsing with empty tokens, JSON arrays, comma spam, and whitespace."""
        # Comma spam and whitespace
        s1 = GatewaySettings(api_keys="  key1 , ,  key2,key3 ,  ,,  ")
        assert s1.api_keys == ["key1", "key2", "key3"]
        assert s1.gateway_api_keys == ["key1", "key2", "key3"]

        # Valid JSON list string
        s2 = GatewaySettings(api_keys='["json-k1", "json-k2", "json-k3"]')
        assert s2.api_keys == ["json-k1", "json-k2", "json-k3"]

        # Malformed JSON (unclosed bracket) -> should gracefully fallback to comma splitting
        s3 = GatewaySettings(api_keys='["malformed-k1", "malformed-k2"')
        assert len(s3.api_keys) > 0

        # Empty string -> empty list
        s4 = GatewaySettings(api_keys="")
        assert s4.api_keys == []

        # Single key
        s5 = GatewaySettings(api_keys="solo-key-123")
        assert s5.api_keys == ["solo-key-123"]

        # Tuple or set input
        s6 = GatewaySettings(api_keys=("t1", "t2"))
        assert s6.api_keys == ["t1", "t2"]

    def test_cors_origins_malformed_and_extreme_formats(self):
        """Test CORS origins parsing."""
        # Comma separated with spaces
        c1 = GatewaySettings(cors_origins=" http://localhost:3000 , https://app.example.com , ")
        assert c1.cors_origins == ["http://localhost:3000", "https://app.example.com"]

        # JSON array
        c2 = GatewaySettings(cors_origins='["http://localhost:8080", "https://api.domain.io"]')
        assert c2.cors_origins == ["http://localhost:8080", "https://api.domain.io"]

        # Empty string
        c3 = GatewaySettings(cors_origins="")
        assert c3.cors_origins == []

    def test_invalid_types_raise_validation_errors(self, monkeypatch):
        """Verify invalid type assignments raise Pydantic ValidationError."""
        # Port not int
        with pytest.raises(ValidationError):
            GatewaySettings(port="not_a_number")

        # Context window not int
        with pytest.raises(ValidationError):
            LLMSettings(context_window="invalid_int")

        # Timeout not float
        with pytest.raises(ValidationError):
            LLMSettings(timeout_seconds="not_a_float")

        # Embedding dimension not int
        with pytest.raises(ValidationError):
            EmbeddingSettings(dimension="dimension_string")

        # DB pool size not int
        with pytest.raises(ValidationError):
            DatabaseSettings(pool_size="twenty")

    def test_extreme_numeric_limits(self):
        """Test configurations with extreme numeric boundary values."""
        llm = LLMSettings(
            context_window=100_000_000,
            timeout_seconds=86400.0,
            temperature=0.0,
            max_tokens=2_000_000,
        )
        assert llm.context_window == 100_000_000
        assert llm.timeout_seconds == 86400.0
        assert llm.temperature == 0.0
        assert llm.max_tokens == 2_000_000

        embed = EmbeddingSettings(
            dimension=4096,
            batch_size=1024,
            timeout_seconds=0.1,
        )
        assert embed.dimension == 4096
        assert embed.batch_size == 1024
        assert embed.timeout_seconds == 0.1

        db = DatabaseSettings(
            pool_size=1000,
            max_overflow=500,
            pool_timeout=0.5,
            pool_recycle=60,
        )
        assert db.pool_size == 1000
        assert db.max_overflow == 500

    def test_decoupled_configuration_overrides(self, monkeypatch):
        """Verify strict independence of decoupled configuration blocks."""
        monkeypatch.setenv("LLM_URL", "http://independent-llm:8000")
        monkeypatch.setenv("EMBEDDING_URL", "http://independent-embed:7000")
        monkeypatch.setenv("STORAGE_DIR", "/custom/storage/path")

        app_settings = AppSettings()
        assert app_settings.llm.url == "http://independent-llm:8000"
        assert app_settings.embedding.url == "http://independent-embed:7000"
        assert app_settings.gateway.storage_dir == "/custom/storage/path"

        # Explicit kwargs override env
        custom = AppSettings(
            llm={"url": "http://override-llm:9000"},
            embedding={"url": "http://override-embed:9001"},
        )
        assert custom.llm.url == "http://override-llm:9000"
        assert custom.embedding.url == "http://override-embed:9001"
