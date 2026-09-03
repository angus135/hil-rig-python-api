"""Versioned SQLite persistence for captured HIL-RIG result data."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from hilrig.exceptions import CaptureSchemaError, CaptureStorageError
from hilrig.models.execution import CompiledAssertion, IRScalar, immutable_fields
from hilrig.results.models import (
    ORIGINAL_ASSERTION_SET_ID,
    RESULT_IR_SCHEMA_VERSION,
    ApplicationErrorRecord,
    CapturedApplicationError,
    CapturedAssertionSet,
    CapturedRunMetadata,
    CapturedTickResult,
    CaptureStatus,
    CommunicationCapture,
    CommunicationPeripheral,
    CommunicationResult,
    PWMMeasurement,
    TickCondition,
    TickResult,
)

_SCHEMA = """
CREATE TABLE result_ir_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version TEXT NOT NULL
);

CREATE TABLE run_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    test_id_hex TEXT NOT NULL CHECK (length(test_id_hex) = 32),
    run_id_hex TEXT NOT NULL CHECK (length(run_id_hex) = 32),
    test_name TEXT NOT NULL,
    tick_period_ns INTEGER NOT NULL CHECK (tick_period_ns > 0),
    expected_tick_count INTEGER NOT NULL CHECK (expected_tick_count > 0),
    received_tick_count INTEGER NOT NULL DEFAULT 0 CHECK (received_tick_count >= 0),
    first_tick INTEGER CHECK (first_tick >= 0 OR first_tick IS NULL),
    last_tick INTEGER CHECK (last_tick >= 0 OR last_tick IS NULL),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finalized_at TEXT,
    application_protocol_version TEXT,
    firmware_version TEXT
);

CREATE TABLE assertion_sets (
    assertion_set_id TEXT PRIMARY KEY CHECK (length(trim(assertion_set_id)) > 0),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    compiled_ir_version TEXT NOT NULL CHECK (length(trim(compiled_ir_version)) > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE assertion_definitions (
    assertion_set_id TEXT NOT NULL,
    assertion_id INTEGER NOT NULL CHECK (assertion_id >= 0),
    peripheral TEXT NOT NULL CHECK (length(trim(peripheral)) > 0),
    channel INTEGER NOT NULL CHECK (channel >= 0),
    operation TEXT NOT NULL CHECK (length(trim(operation)) > 0),
    arguments_json TEXT NOT NULL,
    PRIMARY KEY (assertion_set_id, assertion_id),
    FOREIGN KEY (assertion_set_id) REFERENCES assertion_sets (assertion_set_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE tick_results (
    tick INTEGER PRIMARY KEY CHECK (tick >= 0),
    condition TEXT NOT NULL,
    problem_detail INTEGER,
    digital_input_0 INTEGER CHECK (digital_input_0 IN (0, 1) OR digital_input_0 IS NULL),
    digital_input_1 INTEGER CHECK (digital_input_1 IN (0, 1) OR digital_input_1 IS NULL),
    digital_input_2 INTEGER CHECK (digital_input_2 IN (0, 1) OR digital_input_2 IS NULL),
    digital_input_3 INTEGER CHECK (digital_input_3 IN (0, 1) OR digital_input_3 IS NULL),
    digital_input_4 INTEGER CHECK (digital_input_4 IN (0, 1) OR digital_input_4 IS NULL),
    digital_input_5 INTEGER CHECK (digital_input_5 IN (0, 1) OR digital_input_5 IS NULL),
    digital_input_6 INTEGER CHECK (digital_input_6 IN (0, 1) OR digital_input_6 IS NULL),
    digital_input_7 INTEGER CHECK (digital_input_7 IN (0, 1) OR digital_input_7 IS NULL),
    digital_input_8 INTEGER CHECK (digital_input_8 IN (0, 1) OR digital_input_8 IS NULL),
    digital_input_9 INTEGER CHECK (digital_input_9 IN (0, 1) OR digital_input_9 IS NULL),
    analogue_input_0_uv INTEGER,
    analogue_input_1_uv INTEGER,
    pwm_input_0_period_ns INTEGER CHECK (
        pwm_input_0_period_ns >= 0 OR pwm_input_0_period_ns IS NULL
    ),
    pwm_input_0_duty_permyriad INTEGER CHECK (
        pwm_input_0_duty_permyriad BETWEEN 0 AND 10000
        OR pwm_input_0_duty_permyriad IS NULL
    ),
    pwm_input_1_period_ns INTEGER CHECK (
        pwm_input_1_period_ns >= 0 OR pwm_input_1_period_ns IS NULL
    ),
    pwm_input_1_duty_permyriad INTEGER CHECK (
        pwm_input_1_duty_permyriad BETWEEN 0 AND 10000
        OR pwm_input_1_duty_permyriad IS NULL
    )
);

CREATE TABLE communication_results (
    capture_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tick INTEGER NOT NULL CHECK (tick >= 0),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    peripheral TEXT NOT NULL,
    channel INTEGER NOT NULL CHECK (channel >= 0),
    payload BLOB NOT NULL,
    UNIQUE (tick, peripheral, channel, ordinal)
);

CREATE INDEX communication_result_lookup
ON communication_results (peripheral, channel, tick, ordinal);

CREATE TABLE application_errors (
    error_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tick INTEGER CHECK (tick >= 0 OR tick IS NULL),
    category TEXT NOT NULL,
    recoverable INTEGER NOT NULL CHECK (recoverable IN (0, 1)),
    detail TEXT NOT NULL,
    diagnostic_data BLOB NOT NULL
);

CREATE INDEX application_error_tick_lookup ON application_errors (tick);
"""

_ASSERTION_INSERT = """
INSERT INTO assertion_definitions (
    assertion_set_id, assertion_id, peripheral, channel, operation, arguments_json
) VALUES (?, ?, ?, ?, ?, ?)
"""

_TICK_INSERT = """
INSERT INTO tick_results (
    tick, condition, problem_detail,
    digital_input_0, digital_input_1, digital_input_2, digital_input_3, digital_input_4,
    digital_input_5, digital_input_6, digital_input_7, digital_input_8, digital_input_9,
    analogue_input_0_uv, analogue_input_1_uv,
    pwm_input_0_period_ns, pwm_input_0_duty_permyriad,
    pwm_input_1_period_ns, pwm_input_1_duty_permyriad
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_COMMUNICATION_INSERT = """
INSERT INTO communication_results (tick, ordinal, peripheral, channel, payload)
VALUES (?, ?, ?, ?, ?)
"""

_ERROR_INSERT = """
INSERT INTO application_errors (tick, category, recoverable, detail, diagnostic_data)
VALUES (?, ?, ?, ?, ?)
"""

_REQUIRED_TABLES = frozenset(
    {
        "application_errors",
        "assertion_definitions",
        "assertion_sets",
        "communication_results",
        "result_ir_schema",
        "run_metadata",
        "tick_results",
    }
)


def initialize_capture_database(
    path: Path,
    *,
    test_id: int,
    run_id: int,
    test_name: str,
    tick_period_ns: int,
    expected_tick_count: int,
    compiled_ir_version: str,
    compiled_assertions: Sequence[CompiledAssertion],
    application_protocol_version: str | None,
    firmware_version: str | None,
) -> None:
    """Create a new capture database without overwriting an existing file."""
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    try:
        resolved.touch(exist_ok=False)
    except FileExistsError:
        raise FileExistsError(f"Capture database already exists: {resolved}") from None

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(resolved)
        _configure_write_connection(connection)
        connection.executescript(_SCHEMA)
        connection.execute(
            "INSERT INTO result_ir_schema (singleton, schema_version) VALUES (1, ?)",
            (RESULT_IR_SCHEMA_VERSION,),
        )
        connection.execute(
            """
            INSERT INTO run_metadata (
                singleton, test_id_hex, run_id_hex, test_name, tick_period_ns,
                expected_tick_count, status, created_at,
                application_protocol_version, firmware_version
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"{test_id:032x}",
                f"{run_id:032x}",
                test_name,
                tick_period_ns,
                expected_tick_count,
                CaptureStatus.IN_PROGRESS.value,
                _utc_now(),
                application_protocol_version,
                firmware_version,
            ),
        )
        assertion_set_created_at = _utc_now()
        connection.execute(
            """
            INSERT INTO assertion_sets (
                assertion_set_id, name, compiled_ir_version, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                ORIGINAL_ASSERTION_SET_ID,
                "Original compiled assertions",
                compiled_ir_version,
                assertion_set_created_at,
            ),
        )
        connection.executemany(
            _ASSERTION_INSERT,
            (
                _assertion_parameters(assertion)
                for assertion in _validated_assertions(compiled_assertions)
            ),
        )
        connection.commit()
    except Exception:
        if connection is not None:
            connection.close()
        if resolved.exists():
            resolved.unlink()
        raise
    else:
        connection.close()


class SQLiteCaptureWriter:
    """Single-thread-owned, batched SQLite writer."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._connection = sqlite3.connect(path)
        _configure_write_connection(self._connection)
        _validate_schema(self._connection)

    def write_batch(
        self,
        ticks: Sequence[TickResult],
        communications: Sequence[CommunicationResult],
        errors: Sequence[ApplicationErrorRecord],
    ) -> None:
        """Atomically commit one mixed batch of captured records."""
        if not ticks and not communications and not errors:
            return
        try:
            self._connection.execute("BEGIN")
            if ticks:
                self._connection.executemany(
                    _TICK_INSERT,
                    (_tick_parameters(item) for item in ticks),
                )
                first_tick = min(item.tick for item in ticks)
                last_tick = max(item.tick for item in ticks)
                self._connection.execute(
                    """
                    UPDATE run_metadata
                    SET
                        received_tick_count = received_tick_count + ?,
                        first_tick = CASE
                            WHEN first_tick IS NULL OR first_tick > ? THEN ?
                            ELSE first_tick
                        END,
                        last_tick = CASE
                            WHEN last_tick IS NULL OR last_tick < ? THEN ?
                            ELSE last_tick
                        END
                    WHERE singleton = 1
                    """,
                    (len(ticks), first_tick, first_tick, last_tick, last_tick),
                )
            if communications:
                self._connection.executemany(
                    _COMMUNICATION_INSERT,
                    (
                        (
                            item.tick,
                            item.ordinal,
                            item.peripheral.value,
                            item.channel,
                            sqlite3.Binary(item.payload),
                        )
                        for item in communications
                    ),
                )
            if errors:
                self._connection.executemany(
                    _ERROR_INSERT,
                    (
                        (
                            item.tick,
                            item.category,
                            int(item.recoverable),
                            item.detail,
                            sqlite3.Binary(item.diagnostic_data),
                        )
                        for item in errors
                    ),
                )
            self._connection.commit()
        except sqlite3.Error as error:
            self._connection.rollback()
            raise CaptureStorageError(f"Could not commit captured result batch: {error}") from error

    def finalize(self, requested_status: CaptureStatus | None) -> CaptureStatus:
        """Mark the capture terminal and return the status written to metadata."""
        row = self._connection.execute(
            """
            SELECT
                expected_tick_count,
                received_tick_count,
                first_tick,
                last_tick
            FROM run_metadata
            WHERE singleton = 1
            """
        ).fetchone()
        if row is None:
            raise CaptureSchemaError("Capture database has no run metadata")
        expected, received, first_tick, last_tick = row
        fixed_ticks_complete = (
            received == expected and first_tick == 0 and last_tick == expected - 1
        )
        status = requested_status or (
            CaptureStatus.COMPLETE if fixed_ticks_complete else CaptureStatus.INCOMPLETE
        )
        if status is CaptureStatus.IN_PROGRESS:
            raise ValueError("A finalized capture cannot remain in progress")
        if status is CaptureStatus.COMPLETE and not fixed_ticks_complete:
            raise CaptureStorageError(
                "A capture cannot be marked complete until every expected fixed tick is stored"
            )
        self._connection.execute(
            "UPDATE run_metadata SET status = ?, finalized_at = ? WHERE singleton = 1",
            (status.value, _utc_now()),
        )
        self._connection.commit()
        return status

    def close(self) -> None:
        self._connection.close()


def validate_capture_database(path: Path) -> None:
    """Raise if ``path`` is not a supported capture database."""
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        with closing(_read_connection(path)) as connection:
            _validate_schema(connection)
    except sqlite3.Error as error:
        raise CaptureSchemaError(f"Could not read capture database schema: {error}") from error


def read_metadata(path: Path) -> CapturedRunMetadata:
    """Read the current run summary, including a live fixed-tick count."""
    with closing(_read_connection(path)) as connection:
        row = connection.execute(
            """
            SELECT
                s.schema_version,
                m.test_id_hex,
                m.run_id_hex,
                m.test_name,
                m.tick_period_ns,
                m.expected_tick_count,
                m.received_tick_count,
                m.first_tick,
                m.last_tick,
                m.status,
                m.created_at,
                m.finalized_at,
                m.application_protocol_version,
                m.firmware_version
            FROM run_metadata AS m
            JOIN result_ir_schema AS s ON s.singleton = 1
            WHERE m.singleton = 1
            """
        ).fetchone()
    if row is None:
        raise CaptureSchemaError("Capture database has no run metadata")
    return CapturedRunMetadata(
        schema_version=row[0],
        test_id=int(row[1], 16),
        run_id=int(row[2], 16),
        test_name=row[3],
        tick_period_ns=row[4],
        expected_tick_count=row[5],
        received_tick_count=row[6],
        first_tick=row[7],
        last_tick=row[8],
        status=CaptureStatus(row[9]),
        created_at=row[10],
        finalized_at=row[11],
        application_protocol_version=row[12],
        firmware_version=row[13],
    )


def read_assertion_set(path: Path, assertion_set_id: str) -> CapturedAssertionSet:
    """Read one stored compiled assertion set and its ordered definitions."""
    with closing(_read_connection(path)) as connection:
        set_row = connection.execute(
            """
            SELECT assertion_set_id, name, compiled_ir_version, created_at
            FROM assertion_sets
            WHERE assertion_set_id = ?
            """,
            (assertion_set_id,),
        ).fetchone()
        if set_row is None:
            raise KeyError(f"Unknown assertion set: {assertion_set_id}")
        assertions = _read_assertions(connection, assertion_set_id)
    return CapturedAssertionSet(
        assertion_set_id=str(set_row[0]),
        name=str(set_row[1]),
        compiled_ir_version=str(set_row[2]),
        created_at=str(set_row[3]),
        assertions=assertions,
    )


def iter_assertion_sets(path: Path) -> Iterator[CapturedAssertionSet]:
    """Return stored assertion sets in deterministic creation order."""
    with closing(_read_connection(path)) as connection:
        rows = connection.execute(
            """
            SELECT assertion_set_id, name, compiled_ir_version, created_at
            FROM assertion_sets
            ORDER BY created_at, assertion_set_id
            """
        ).fetchall()
        snapshots = tuple(
            CapturedAssertionSet(
                assertion_set_id=str(row[0]),
                name=str(row[1]),
                compiled_ir_version=str(row[2]),
                created_at=str(row[3]),
                assertions=_read_assertions(connection, str(row[0])),
            )
            for row in rows
        )
    return iter(snapshots)


def read_tick(path: Path, tick: int) -> CapturedTickResult | None:
    """Read one fixed tick row."""
    with closing(_read_connection(path)) as connection:
        row = connection.execute("SELECT * FROM tick_results WHERE tick = ?", (tick,)).fetchone()
    return None if row is None else _captured_tick(row)


def iter_ticks(
    path: Path,
    *,
    from_tick: int,
    until_tick: int | None,
) -> Iterator[CapturedTickResult]:
    """Stream fixed tick rows in chronological order over an inclusive range."""
    query = "SELECT * FROM tick_results WHERE tick >= ?"
    parameters: list[object] = [from_tick]
    if until_tick is not None:
        query += " AND tick <= ?"
        parameters.append(until_tick)
    query += " ORDER BY tick"
    with closing(_read_connection(path)) as connection:
        cursor = connection.execute(query, parameters)
        for row in cursor:
            yield _captured_tick(row)


def iter_communications(
    path: Path,
    *,
    tick_period_ns: int,
    peripheral: CommunicationPeripheral | None,
    channel: int | None,
    from_tick: int,
    until_tick: int | None,
) -> Iterator[CommunicationCapture]:
    """Stream matching raw communication captures in deterministic order."""
    clauses = ["tick >= ?"]
    parameters: list[object] = [from_tick]
    if until_tick is not None:
        clauses.append("tick <= ?")
        parameters.append(until_tick)
    if peripheral is not None:
        clauses.append("peripheral = ?")
        parameters.append(peripheral.value)
    if channel is not None:
        clauses.append("channel = ?")
        parameters.append(channel)
    query = (
        "SELECT capture_id, tick, ordinal, peripheral, channel, payload "
        f"FROM communication_results WHERE {' AND '.join(clauses)} "
        "ORDER BY tick, capture_id"
    )
    with closing(_read_connection(path)) as connection:
        for row in connection.execute(query, parameters):
            yield CommunicationCapture(
                capture_id=row[0],
                tick=row[1],
                time_ns=row[1] * tick_period_ns,
                ordinal=row[2],
                peripheral=CommunicationPeripheral(row[3]),
                channel=row[4],
                payload=bytes(row[5]),
            )


def iter_application_errors(path: Path) -> Iterator[CapturedApplicationError]:
    """Stream stored application diagnostics in arrival order."""
    with closing(_read_connection(path)) as connection:
        rows = connection.execute(
            """
            SELECT error_id, category, detail, recoverable, tick, diagnostic_data
            FROM application_errors
            ORDER BY error_id
            """
        )
        for row in rows:
            yield CapturedApplicationError(
                error_id=row[0],
                category=row[1],
                detail=row[2],
                recoverable=bool(row[3]),
                tick=row[4],
                diagnostic_data=bytes(row[5]),
            )


def _configure_write_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 5000")


def _read_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _validate_schema(connection: sqlite3.Connection) -> None:
    try:
        row = connection.execute(
            "SELECT schema_version FROM result_ir_schema WHERE singleton = 1"
        ).fetchone()
    except sqlite3.Error as error:
        raise CaptureSchemaError("File is not a HIL-RIG capture database") from error
    if row is None:
        raise CaptureSchemaError("Capture database has no schema version")
    if row[0] != RESULT_IR_SCHEMA_VERSION:
        raise CaptureSchemaError(
            f"Unsupported capture schema {row[0]!r}; expected {RESULT_IR_SCHEMA_VERSION!r}"
        )
    table_names = {
        str(table[0])
        for table in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing_tables = sorted(_REQUIRED_TABLES - table_names)
    if missing_tables:
        raise CaptureSchemaError(
            f"Capture database is missing required tables: {', '.join(missing_tables)}"
        )
    original_set = connection.execute(
        "SELECT 1 FROM assertion_sets WHERE assertion_set_id = ?",
        (ORIGINAL_ASSERTION_SET_ID,),
    ).fetchone()
    if original_set is None:
        raise CaptureSchemaError("Capture database has no original assertion set")


def _validated_assertions(
    assertions: Sequence[CompiledAssertion],
) -> Iterator[CompiledAssertion]:
    if isinstance(assertions, (str, bytes)) or not isinstance(assertions, Sequence):
        raise TypeError("compiled_assertions must be a sequence of CompiledAssertion values")
    for expected_id, assertion in enumerate(assertions):
        if not isinstance(assertion, CompiledAssertion):
            raise TypeError("compiled_assertions must contain only CompiledAssertion values")
        if assertion.assertion_id != expected_id:
            raise ValueError("Stored assertion IDs must be sequential from zero")
        yield assertion


def _assertion_parameters(assertion: CompiledAssertion) -> tuple[object, ...]:
    try:
        arguments_json = json.dumps(
            dict(assertion.arguments),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Assertion {assertion.assertion_id} arguments are not valid IR scalar values"
        ) from error
    return (
        ORIGINAL_ASSERTION_SET_ID,
        assertion.assertion_id,
        assertion.peripheral,
        assertion.channel,
        assertion.assertion,
        arguments_json,
    )


def _read_assertions(
    connection: sqlite3.Connection,
    assertion_set_id: str,
) -> tuple[CompiledAssertion, ...]:
    rows = connection.execute(
        """
        SELECT assertion_id, peripheral, channel, operation, arguments_json
        FROM assertion_definitions
        WHERE assertion_set_id = ?
        ORDER BY assertion_id
        """,
        (assertion_set_id,),
    ).fetchall()
    assertions: list[CompiledAssertion] = []
    for expected_id, row in enumerate(rows):
        assertion_id = int(row[0])
        if assertion_id != expected_id:
            raise CaptureSchemaError(
                f"Assertion IDs in set {assertion_set_id!r} must be sequential from zero"
            )
        arguments = _decode_assertion_arguments(row[4], assertion_id=assertion_id)
        assertions.append(
            CompiledAssertion(
                assertion_id=assertion_id,
                peripheral=str(row[1]),
                channel=int(row[2]),
                assertion=str(row[3]),
                arguments=immutable_fields(arguments),
            )
        )
    return tuple(assertions)


def _decode_assertion_arguments(value: object, *, assertion_id: int) -> dict[str, IRScalar]:
    if not isinstance(value, str):
        raise CaptureSchemaError(f"Assertion {assertion_id} arguments must be JSON text")
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, ValueError) as error:
        raise CaptureSchemaError(f"Assertion {assertion_id} has invalid arguments JSON") from error
    if not isinstance(decoded, dict):
        raise CaptureSchemaError(f"Assertion {assertion_id} arguments must be a JSON object")

    arguments: dict[str, IRScalar] = {}
    for name, argument in decoded.items():
        if not isinstance(name, str) or not _is_ir_scalar(argument):
            raise CaptureSchemaError(
                f"Assertion {assertion_id} arguments must contain only IR scalar values"
            )
        arguments[name] = argument
    return arguments


def _is_ir_scalar(value: object) -> bool:
    return (
        value is None
        or isinstance(value, (str, int, bool))
        or (isinstance(value, float) and math.isfinite(value))
    )


def _tick_parameters(item: TickResult) -> tuple[object, ...]:
    pwm_values: list[int | None] = []
    for measurement in item.pwm_inputs:
        if measurement is None:
            pwm_values.extend((None, None))
        else:
            pwm_values.extend((measurement.period_ns, measurement.duty_permyriad))
    return (
        item.tick,
        item.condition.value,
        item.problem_detail,
        *(None if value is None else int(value) for value in item.digital_inputs),
        *item.analogue_inputs_uv,
        *pwm_values,
    )


def _captured_tick(row: sqlite3.Row | tuple[object, ...]) -> CapturedTickResult:
    digital = tuple(None if value is None else bool(value) for value in row[3:13])
    analogue = tuple(row[13:15])
    pwm = (
        _pwm_measurement(row[15], row[16]),
        _pwm_measurement(row[17], row[18]),
    )
    return CapturedTickResult(
        tick=int(row[0]),
        condition=TickCondition(str(row[1])),
        problem_detail=None if row[2] is None else int(row[2]),
        digital_inputs=digital,
        analogue_inputs_uv=analogue,
        pwm_inputs=pwm,
    )


def _pwm_measurement(period_ns: object, duty_permyriad: object) -> PWMMeasurement | None:
    if period_ns is None or duty_permyriad is None:
        return None
    return PWMMeasurement(period_ns=int(period_ns), duty_permyriad=int(duty_permyriad))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
