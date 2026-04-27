from __future__ import annotations

import argparse
import base64
import curses
import json
import textwrap
from dataclasses import dataclass, field
from typing import Any
from urllib import error, parse, request


HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head")
AUTH_MODES = ("none", "basic", "bearer", "header", "query")
PROMPT_LINE_OFFSET = 2


@dataclass(slots=True)
class ParameterSpec:
	name: str
	location: str
	required: bool
	schema_type: str = ""
	description: str = ""


@dataclass(slots=True)
class RequestBodySpec:
	content_type: str
	required: bool
	description: str = ""


@dataclass(slots=True)
class Operation:
	method: str
	path: str
	title: str
	description: str
	operation_id: str
	parameters: list[ParameterSpec] = field(default_factory=list)
	request_body: RequestBodySpec | None = None

	@property
	def key(self) -> str:
		return f"{self.method.upper()} {self.path}"


@dataclass(slots=True)
class AuthSettings:
	mode: str = "none"
	username: str = ""
	password: str = ""
	token: str = ""
	header_name: str = "Authorization"
	header_value: str = ""
	query_name: str = "api_key"
	query_value: str = ""

	def summary(self) -> str:
		if self.mode == "basic":
			return f"basic ({self.username or 'no user'})"
		if self.mode == "bearer":
			return "bearer" if self.token else "bearer (empty token)"
		if self.mode == "header":
			if self.header_name and self.header_value:
				return f"header {self.header_name}"
			return "header (incomplete)"
		if self.mode == "query":
			if self.query_name and self.query_value:
				return f"query {self.query_name}"
			return "query (incomplete)"
		return "none"


@dataclass(slots=True)
class ResponseView:
	title: str = "No request sent yet."
	body: str = ""
	headers: list[tuple[str, str]] = field(default_factory=list)

	def to_lines(self, width: int) -> list[str]:
		lines = [self.title, ""]
		if self.headers:
			lines.append("Headers:")
			for name, value in self.headers:
				lines.extend(wrap_line(f"{name}: {value}", width))
			lines.append("")
		if self.body:
			lines.append("Body:")
			for raw_line in self.body.splitlines() or [self.body]:
				lines.extend(wrap_line(raw_line, width))
		return lines


def wrap_line(text: str, width: int) -> list[str]:
	normalized = text or ""
	if width <= 1:
		return [normalized[:1]]
	wrapped = textwrap.wrap(
		normalized,
		width=width,
		replace_whitespace=False,
		drop_whitespace=False,
		break_long_words=True,
		break_on_hyphens=False,
	)
	return wrapped or [""]


def normalize_service_url(raw_url: str) -> tuple[str, str]:
	raw_value = (raw_url or "").strip()
	if not raw_value:
		raw_value = "http://localhost:8000"
	if "://" not in raw_value:
		raw_value = f"http://{raw_value}"

	parsed = parse.urlsplit(raw_value)
	path = parsed.path.rstrip("/")

	if path.endswith("/openapi.json"):
		base_path = path[: -len("/openapi.json")]
		spec_path = path
	elif path.endswith("/docs"):
		base_path = path[: -len("/docs")]
		spec_path = f"{base_path}/openapi.json" if base_path else "/openapi.json"
	elif path.endswith("/redoc"):
		base_path = path[: -len("/redoc")]
		spec_path = f"{base_path}/openapi.json" if base_path else "/openapi.json"
	else:
		base_path = path
		spec_path = f"{base_path}/openapi.json" if base_path else "/openapi.json"

	service_parts = (parsed.scheme, parsed.netloc, base_path or "", "", "")
	spec_parts = (parsed.scheme, parsed.netloc, spec_path, "", "")

	service_url = parse.urlunsplit(service_parts).rstrip("/")
	spec_url = parse.urlunsplit(spec_parts)
	return service_url, spec_url


def decode_body(payload: bytes, headers: Any) -> str:
	content_type = headers.get("Content-Type", "")
	charset = "utf-8"
	if "charset=" in content_type:
		charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()
	text = payload.decode(charset or "utf-8", errors="replace")
	if "json" in content_type.lower():
		try:
			return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
		except json.JSONDecodeError:
			return text
	return text


def fetch_openapi_document(spec_url: str) -> dict[str, Any]:
	req = request.Request(
		spec_url,
		headers={
			"Accept": "application/json",
			"User-Agent": "openapi-tui-client/1.0",
		},
		method="GET",
	)
	with request.urlopen(req, timeout=15) as response:
		payload = response.read()
	return json.loads(payload.decode("utf-8"))


def pick_default_auth_mode(document: dict[str, Any]) -> str:
	schemes = document.get("components", {}).get("securitySchemes", {})
	for scheme in schemes.values():
		scheme_type = scheme.get("type")
		if scheme_type == "http":
			http_scheme = (scheme.get("scheme") or "").lower()
			if http_scheme == "basic":
				return "basic"
			if http_scheme == "bearer":
				return "bearer"
		if scheme_type == "apiKey":
			where = (scheme.get("in") or "").lower()
			if where == "header":
				return "header"
			if where == "query":
				return "query"
	return "none"


def resolve_request_base(document: dict[str, Any], spec_url: str, fallback_url: str) -> str:
	servers = document.get("servers") or []
	if not servers:
		return fallback_url
	candidate = (servers[0] or {}).get("url")
	if not candidate:
		return fallback_url
	return parse.urljoin(spec_url, candidate).rstrip("/")


def parse_parameters(raw_parameters: list[dict[str, Any]]) -> list[ParameterSpec]:
	deduped: dict[tuple[str, str], ParameterSpec] = {}
	for raw in raw_parameters:
		name = raw.get("name")
		location = raw.get("in")
		if not name or not location:
			continue
		schema = raw.get("schema") or {}
		deduped[(name, location)] = ParameterSpec(
			name=name,
			location=location,
			required=bool(raw.get("required")),
			schema_type=str(schema.get("type") or ""),
			description=raw.get("description") or "",
		)
	return list(deduped.values())


def parse_request_body(raw_body: dict[str, Any]) -> RequestBodySpec | None:
	content = raw_body.get("content") or {}
	if not content:
		return None
	if "application/json" in content:
		content_type = "application/json"
	else:
		content_type = next(iter(content))
	body_meta = content.get(content_type) or {}
	return RequestBodySpec(
		content_type=content_type,
		required=bool(raw_body.get("required")),
		description=body_meta.get("description") or raw_body.get("description") or "",
	)


def parse_operations(document: dict[str, Any]) -> list[Operation]:
	operations: list[Operation] = []
	paths = document.get("paths") or {}
	for path, path_item in paths.items():
		if not isinstance(path_item, dict):
			continue
		shared_parameters = path_item.get("parameters") or []
		for method in HTTP_METHODS:
			raw_operation = path_item.get(method)
			if not isinstance(raw_operation, dict):
				continue
			raw_parameters = list(shared_parameters) + list(raw_operation.get("parameters") or [])
			summary = (
				raw_operation.get("summary")
				or raw_operation.get("operationId")
				or f"{method.upper()} {path}"
			)
			description = raw_operation.get("description") or ""
			operations.append(
				Operation(
					method=method.upper(),
					path=path,
					title=summary,
					description=description,
					operation_id=raw_operation.get("operationId") or summary,
					parameters=parse_parameters(raw_parameters),
					request_body=parse_request_body(raw_operation.get("requestBody") or {}),
				)
			)
	operations.sort(key=lambda item: (item.path, item.method))
	return operations


def parameter_key(parameter: ParameterSpec) -> str:
	return f"{parameter.location}:{parameter.name}"


def render_parameter_label(parameter: ParameterSpec) -> str:
	required = "required" if parameter.required else "optional"
	schema_type = f" [{parameter.schema_type}]" if parameter.schema_type else ""
	return f"{parameter.location}:{parameter.name} ({required}){schema_type}"


def prepare_request(
	base_url: str,
	operation: Operation,
	values: dict[str, str],
	auth: AuthSettings,
) -> tuple[str, dict[str, str], bytes | None]:
	path = operation.path
	query_pairs: list[tuple[str, str]] = []
	headers: dict[str, str] = {"Accept": "application/json, text/plain, */*"}

	for parameter in operation.parameters:
		key = parameter_key(parameter)
		value = (values.get(key) or "").strip()
		if parameter.required and not value:
			raise ValueError(f"Missing required field: {parameter.location}:{parameter.name}")
		if not value:
			continue
		if parameter.location == "path":
			path = path.replace(f"{{{parameter.name}}}", parse.quote(value, safe=""))
		elif parameter.location == "query":
			query_pairs.append((parameter.name, value))
		elif parameter.location == "header":
			headers[parameter.name] = value
		elif parameter.location == "cookie":
			existing = headers.get("Cookie", "")
			prefix = f"{existing}; " if existing else ""
			headers["Cookie"] = f"{prefix}{parameter.name}={value}"

	if auth.mode == "basic":
		encoded = base64.b64encode(f"{auth.username}:{auth.password}".encode("utf-8")).decode("ascii")
		headers["Authorization"] = f"Basic {encoded}"
	elif auth.mode == "bearer":
		if auth.token:
			headers["Authorization"] = f"Bearer {auth.token}"
	elif auth.mode == "header":
		if auth.header_name and auth.header_value:
			headers[auth.header_name] = auth.header_value
	elif auth.mode == "query":
		if auth.query_name and auth.query_value:
			query_pairs.append((auth.query_name, auth.query_value))

	body_value = (values.get("body") or "").strip()
	payload: bytes | None = None
	if operation.request_body and body_value:
		content_type = operation.request_body.content_type
		headers["Content-Type"] = content_type
		if "json" in content_type:
			parsed_json = json.loads(body_value)
			payload = json.dumps(parsed_json, ensure_ascii=False).encode("utf-8")
		else:
			payload = body_value.encode("utf-8")
	elif operation.request_body and operation.request_body.required:
		raise ValueError("Missing required request body")

	query_string = parse.urlencode(query_pairs, doseq=True)
	target_url = f"{base_url.rstrip('/')}{path}"
	if query_string:
		target_url = f"{target_url}?{query_string}"

	return target_url, headers, payload


def execute_request(
	base_url: str,
	operation: Operation,
	values: dict[str, str],
	auth: AuthSettings,
) -> ResponseView:
	target_url, headers, payload = prepare_request(base_url, operation, values, auth)
	req = request.Request(target_url, data=payload, headers=headers, method=operation.method)
	try:
		with request.urlopen(req, timeout=30) as response:
			response_body = response.read()
			response_headers = list(response.headers.items())
			status = f"{response.status} {response.reason}"
			body_text = decode_body(response_body, response.headers)
			return ResponseView(title=f"{operation.method} {target_url} -> {status}", body=body_text, headers=response_headers)
	except error.HTTPError as exc:
		response_body = exc.read()
		body_text = decode_body(response_body, exc.headers)
		return ResponseView(
			title=f"{operation.method} {target_url} -> {exc.code} {exc.reason}",
			body=body_text,
			headers=list(exc.headers.items()),
		)
	except error.URLError as exc:
		return ResponseView(title=f"Request failed: {exc.reason}")


class OpenApiTui:
	def __init__(self, stdscr: Any, initial_url: str) -> None:
		self.stdscr = stdscr
		self.raw_url = initial_url
		self.service_url, self.spec_url = normalize_service_url(initial_url)
		self.request_base_url = self.service_url
		self.document_title = "OpenAPI"
		self.operations: list[Operation] = []
		self.operation_index = 0
		self.request_index = 0
		self.auth_index = 0
		self.mode = "operations"
		self.status_message = ""
		self.response_view = ResponseView()
		self.form_values: dict[str, dict[str, str]] = {}
		self.auth = AuthSettings()
		self.last_screen_before_auth = "operations"

	def load_document(self) -> None:
		document = fetch_openapi_document(self.spec_url)
		self.document_title = document.get("info", {}).get("title") or "OpenAPI"
		self.request_base_url = resolve_request_base(document, self.spec_url, self.service_url)
		self.operations = parse_operations(document)
		if self.auth.mode == "none":
			self.auth.mode = pick_default_auth_mode(document)
		if not self.operations:
			raise ValueError("OpenAPI document has no operations")
		self.operation_index = min(self.operation_index, len(self.operations) - 1)
		self.request_index = 0
		self.status_message = f"Loaded {len(self.operations)} operations from {self.spec_url}"

	def run(self) -> None:
		self.set_cursor_visibility(0)
		self.stdscr.keypad(True)
		try:
			self.load_document()
		except Exception as exc:
			self.status_message = f"Load failed: {exc}"

		while True:
			self.draw()
			key = self.stdscr.getch()
			if key in (ord("q"), ord("Q")):
				return
			if key in (ord("r"), ord("R")):
				self.handle_reload()
				continue
			if key in (ord("u"), ord("U")):
				self.handle_url_update()
				continue
			if self.mode == "operations":
				self.handle_operations_key(key)
			elif self.mode == "request":
				self.handle_request_key(key)
			elif self.mode == "auth":
				self.handle_auth_key(key)

	def draw(self) -> None:
		self.stdscr.erase()
		if self.mode == "operations":
			self.draw_operations_screen()
		elif self.mode == "request":
			self.draw_request_screen()
		else:
			self.draw_auth_screen()
		self.stdscr.refresh()

	def draw_header(self, lines: list[str]) -> int:
		width = self.stdscr.getmaxyx()[1]
		for index, line in enumerate(lines):
			self.safe_addstr(index, 0, line[: max(width - 1, 1)])
		return len(lines)

	def draw_footer(self, text: str) -> None:
		height, width = self.stdscr.getmaxyx()
		self.safe_addstr(height - 1, 0, text[: max(width - 1, 1)], curses.A_REVERSE)

	def draw_operations_screen(self) -> None:
		header_end = self.draw_header(
			[
				f"API Client TUI | {self.document_title}",
				f"Service: {self.request_base_url or self.service_url}",
				f"Spec: {self.spec_url}",
				"Arrows - select | Enter - open | a - auth | u - change URL | r - reload | q - quit",
			]
		)
		height, width = self.stdscr.getmaxyx()
		available_rows = max(height - header_end - 2, 1)

		if not self.operations:
			self.safe_addstr(header_end + 1, 0, "No operations loaded. Press u to change URL or r to reload.")
			self.draw_footer(self.status_message or "Idle")
			return

		start_index = 0
		if self.operation_index >= available_rows:
			start_index = self.operation_index - available_rows + 1

		visible_operations = self.operations[start_index : start_index + available_rows]
		for offset, operation in enumerate(visible_operations):
			row = header_end + offset
			absolute_index = start_index + offset
			marker = ">" if absolute_index == self.operation_index else " "
			text = f"{marker} {operation.method:<7} {operation.path} | {operation.title}"
			attr = curses.A_REVERSE if absolute_index == self.operation_index else curses.A_NORMAL
			self.safe_addstr(row, 0, text[: max(width - 1, 1)], attr)

		self.draw_footer(self.status_message or "Idle")

	def current_operation(self) -> Operation | None:
		if not self.operations:
			return None
		return self.operations[self.operation_index]

	def request_items(self, operation: Operation) -> list[tuple[str, str, str]]:
		values = self.form_values.setdefault(operation.key, {})
		items: list[tuple[str, str, str]] = []
		for parameter in operation.parameters:
			key = parameter_key(parameter)
			items.append(("field", key, render_parameter_label(parameter)))
			values.setdefault(key, "")
		if operation.request_body:
			items.append(("field", "body", f"body ({operation.request_body.content_type})"))
			if operation.request_body.content_type == "application/json":
				values.setdefault("body", "{}" if operation.request_body.required else "")
			else:
				values.setdefault("body", "")
		items.append(("action", "send", "Send request"))
		items.append(("action", "auth", "Edit auth"))
		items.append(("action", "back", "Back to operations"))
		return items

	def draw_request_screen(self) -> None:
		operation = self.current_operation()
		if operation is None:
			self.mode = "operations"
			return

		values = self.form_values.setdefault(operation.key, {})
		items = self.request_items(operation)
		self.request_index = min(self.request_index, len(items) - 1)

		header_lines = [
			f"{operation.method} {operation.path}",
			f"Title: {operation.title}",
			f"Auth: {self.auth.summary()} | Base URL: {self.request_base_url}",
			"Arrows - move | Enter - edit/open | r - reload | u - change URL | q - quit",
		]
		if operation.description:
			header_lines.append(f"Info: {operation.description}")

		header_end = self.draw_header(header_lines)
		height, width = self.stdscr.getmaxyx()
		list_rows = max((height - header_end) // 2, 4)
		start_index = 0
		if self.request_index >= list_rows:
			start_index = self.request_index - list_rows + 1

		visible_items = items[start_index : start_index + list_rows]
		for offset, item in enumerate(visible_items):
			row = header_end + offset
			absolute_index = start_index + offset
			kind, key, label = item
			if kind == "field":
				current_value = values.get(key, "")
				display_value = current_value if current_value else "<empty>"
				text = f"{label}: {display_value}"
			else:
				text = f"[{label}]"
			attr = curses.A_REVERSE if absolute_index == self.request_index else curses.A_NORMAL
			self.safe_addstr(row, 0, text[: max(width - 1, 1)], attr)

		response_start = header_end + list_rows + 1
		if response_start < height - 1:
			self.safe_addstr(response_start, 0, "Response:")
			response_lines = self.response_view.to_lines(max(width - 2, 20))
			max_response_rows = max(height - response_start - 2, 1)
			truncated = response_lines[:max_response_rows]
			for index, line in enumerate(truncated, start=1):
				self.safe_addstr(response_start + index, 0, line[: max(width - 1, 1)])

		self.draw_footer(self.status_message or "Idle")

	def auth_items(self) -> list[tuple[str, str, str]]:
		items: list[tuple[str, str, str]] = [("field", "mode", "Mode")]
		if self.auth.mode == "basic":
			items.extend(
				[
					("field", "username", "Username"),
					("field", "password", "Password"),
				]
			)
		elif self.auth.mode == "bearer":
			items.append(("field", "token", "Token"))
		elif self.auth.mode == "header":
			items.extend(
				[
					("field", "header_name", "Header name"),
					("field", "header_value", "Header value"),
				]
			)
		elif self.auth.mode == "query":
			items.extend(
				[
					("field", "query_name", "Query name"),
					("field", "query_value", "Query value"),
				]
			)
		items.append(("action", "save", "Save and go back"))
		return items

	def draw_auth_screen(self) -> None:
		items = self.auth_items()
		self.auth_index = min(self.auth_index, len(items) - 1)
		header_end = self.draw_header(
			[
				"Authentication",
				f"Current auth: {self.auth.summary()}",
				"Arrows - move | Enter - edit/cycle | r - reload spec | u - change URL | q - quit",
			]
		)
		height, width = self.stdscr.getmaxyx()
		available_rows = max(height - header_end - 2, 1)
		start_index = 0
		if self.auth_index >= available_rows:
			start_index = self.auth_index - available_rows + 1

		visible_items = items[start_index : start_index + available_rows]
		for offset, item in enumerate(visible_items):
			row = header_end + offset
			absolute_index = start_index + offset
			kind, key, label = item
			if kind == "field":
				value = self.auth_field_value(key)
				if key == "password" and value:
					value = "*" * len(value)
				text = f"{label}: {value or '<empty>'}"
			else:
				text = f"[{label}]"
			attr = curses.A_REVERSE if absolute_index == self.auth_index else curses.A_NORMAL
			self.safe_addstr(row, 0, text[: max(width - 1, 1)], attr)

		self.draw_footer(self.status_message or "Idle")

	def auth_field_value(self, key: str) -> str:
		if key == "mode":
			return self.auth.mode
		return getattr(self.auth, key)

	def handle_reload(self) -> None:
		try:
			current_key = self.current_operation().key if self.current_operation() else ""
			self.load_document()
			if current_key:
				for index, operation in enumerate(self.operations):
					if operation.key == current_key:
						self.operation_index = index
						break
			self.response_view = ResponseView(title="OpenAPI reloaded.")
		except Exception as exc:
			self.status_message = f"Reload failed: {exc}"

	def handle_url_update(self) -> None:
		new_url = self.prompt_input("Service URL", self.raw_url)
		if new_url is None:
			self.status_message = "URL update cancelled"
			return
		candidate_raw_url = new_url.strip() or self.raw_url
		candidate_service_url, candidate_spec_url = normalize_service_url(candidate_raw_url)
		previous_raw_url = self.raw_url
		previous_service_url = self.service_url
		previous_spec_url = self.spec_url
		self.raw_url = candidate_raw_url
		self.service_url = candidate_service_url
		self.spec_url = candidate_spec_url
		try:
			self.load_document()
			self.response_view = ResponseView(title="URL updated and document reloaded.")
		except Exception as exc:
			self.raw_url = previous_raw_url
			self.service_url = previous_service_url
			self.spec_url = previous_spec_url
			self.status_message = f"Load failed: {exc}"

	def handle_operations_key(self, key: int) -> None:
		if not self.operations:
			return
		if key == curses.KEY_UP:
			self.operation_index = max(self.operation_index - 1, 0)
		elif key == curses.KEY_DOWN:
			self.operation_index = min(self.operation_index + 1, len(self.operations) - 1)
		elif key in (curses.KEY_ENTER, 10, 13):
			self.mode = "request"
			self.request_index = 0
			self.response_view = ResponseView()
		elif key in (ord("a"), ord("A")):
			self.last_screen_before_auth = "operations"
			self.mode = "auth"
			self.auth_index = 0

	def handle_request_key(self, key: int) -> None:
		operation = self.current_operation()
		if operation is None:
			self.mode = "operations"
			return
		items = self.request_items(operation)
		if key == curses.KEY_UP:
			self.request_index = max(self.request_index - 1, 0)
		elif key == curses.KEY_DOWN:
			self.request_index = min(self.request_index + 1, len(items) - 1)
		elif key in (ord("a"), ord("A")):
			self.last_screen_before_auth = "request"
			self.mode = "auth"
			self.auth_index = 0
		elif key in (curses.KEY_ENTER, 10, 13):
			kind, item_key, _ = items[self.request_index]
			if kind == "field":
				self.edit_request_field(operation, item_key)
			elif item_key == "send":
				self.send_request(operation)
			elif item_key == "auth":
				self.last_screen_before_auth = "request"
				self.mode = "auth"
				self.auth_index = 0
			elif item_key == "back":
				self.mode = "operations"
				self.request_index = 0

	def handle_auth_key(self, key: int) -> None:
		items = self.auth_items()
		if key == curses.KEY_UP:
			self.auth_index = max(self.auth_index - 1, 0)
		elif key == curses.KEY_DOWN:
			self.auth_index = min(self.auth_index + 1, len(items) - 1)
		elif key in (curses.KEY_ENTER, 10, 13):
			kind, item_key, _ = items[self.auth_index]
			if kind == "field":
				self.edit_auth_field(item_key)
			elif item_key == "save":
				self.mode = self.last_screen_before_auth
				self.status_message = f"Auth updated: {self.auth.summary()}"

	def edit_request_field(self, operation: Operation, item_key: str) -> None:
		values = self.form_values.setdefault(operation.key, {})
		existing = values.get(item_key, "")
		updated = self.prompt_input(item_key, existing)
		if updated is None:
			self.status_message = f"Edit cancelled: {item_key}"
			return
		values[item_key] = updated
		self.status_message = f"Updated {item_key}"

	def edit_auth_field(self, item_key: str) -> None:
		if item_key == "mode":
			current_index = AUTH_MODES.index(self.auth.mode)
			self.auth.mode = AUTH_MODES[(current_index + 1) % len(AUTH_MODES)]
			self.auth_index = 0
			self.status_message = f"Auth mode: {self.auth.mode}"
			return
		existing = getattr(self.auth, item_key)
		updated = self.prompt_input(item_key, existing)
		if updated is None:
			self.status_message = f"Edit cancelled: {item_key}"
			return
		setattr(self.auth, item_key, updated)
		self.status_message = f"Updated {item_key}"

	def send_request(self, operation: Operation) -> None:
		values = self.form_values.setdefault(operation.key, {})
		try:
			self.response_view = execute_request(self.request_base_url, operation, values, self.auth)
			self.status_message = "Request finished"
		except json.JSONDecodeError as exc:
			self.status_message = f"Invalid JSON body: {exc.msg}"
		except ValueError as exc:
			self.status_message = str(exc)

	def prompt_input(self, label: str, initial_value: str) -> str | None:
		height, width = self.stdscr.getmaxyx()
		buffer = list(initial_value)
		position = len(buffer)
		prompt = f"{label}: "
		self.set_cursor_visibility(1)
		while True:
			display = "".join(buffer)
			line = f"{prompt}{display}"
			self.safe_addstr(height - PROMPT_LINE_OFFSET, 0, " " * max(width - 1, 1))
			self.safe_addstr(height - PROMPT_LINE_OFFSET, 0, line[: max(width - 1, 1)], curses.A_REVERSE)
			cursor_x = min(len(prompt) + position, max(width - 2, 0))
			self.stdscr.move(height - PROMPT_LINE_OFFSET, cursor_x)
			self.stdscr.refresh()
			key = self.stdscr.get_wch()

			if key == "\x1b":
				self.set_cursor_visibility(0)
				return None
			if key in ("\n", "\r") or key in (curses.KEY_ENTER, 10, 13):
				self.set_cursor_visibility(0)
				return "".join(buffer)
			if key in (curses.KEY_BACKSPACE, 127, 8, "\b", "\x7f"):
				if position > 0:
					buffer.pop(position - 1)
					position -= 1
				continue
			if key == curses.KEY_LEFT:
				position = max(position - 1, 0)
				continue
			if key == curses.KEY_RIGHT:
				position = min(position + 1, len(buffer))
				continue
			if key == curses.KEY_DC:
				if position < len(buffer):
					buffer.pop(position)
				continue
			if isinstance(key, str) and key.isprintable():
				buffer.insert(position, key)
				position += 1

	def safe_addstr(self, row: int, col: int, text: str, attr: int = 0) -> None:
		height, width = self.stdscr.getmaxyx()
		if row < 0 or row >= height or col >= width:
			return
		clipped = text[: max(width - col - 1, 1)]
		try:
			self.stdscr.addstr(row, col, clipped, attr)
		except curses.error:
			return

	def set_cursor_visibility(self, visibility: int) -> None:
		try:
			curses.curs_set(visibility)
		except curses.error:
			return


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Interactive TUI client for OpenAPI services")
	parser.add_argument(
		"url",
		nargs="?",
		default="http://localhost:8000",
		help="Service URL, /docs URL or /openapi.json URL",
	)
	return parser.parse_args()


def main() -> None:
	args = parse_args()
	curses.wrapper(lambda stdscr: OpenApiTui(stdscr, args.url).run())


if __name__ == "__main__":
	main()
