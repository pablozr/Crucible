export const STATUS_URL = "http://127.0.0.1:7331/v1/status";


export type StatusOptions = {
  json: boolean;
};


export type StatusDeps = {
  fetchFn?: typeof fetch;
  stdout?: (message: string) => void;
};


type StatusDatabase = {
  path: string;
  migration_revision: string;
  journal_mode: string;
  synchronous: string;
  foreign_keys: boolean;
};


type StatusSystem = {
  status: string;
  version: string;
  api_version: string;
  address: string;
  database: StatusDatabase;
};


type StatusEnvelope = {
  status: "ok";
  message: string;
  data: {
    system: StatusSystem;
  };
};


function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}


function parseSuccessEnvelope(value: unknown): StatusEnvelope | undefined {
  if (!isRecord(value)) {
    return undefined;
  }

  if (value["status"] !== "ok" || typeof value["message"] !== "string") {
    return undefined;
  }

  if (!isRecord(value["data"])) {
    return undefined;
  }

  const system = (value["data"] as Record<string, unknown>)["system"];
  if (!isRecord(system)) {
    return undefined;
  }

  const database = system["database"];
  if (!isRecord(database)) {
    return undefined;
  }

  if (
    typeof system["status"] !== "string" ||
    typeof system["version"] !== "string" ||
    typeof system["api_version"] !== "string" ||
    typeof system["address"] !== "string" ||
    typeof database["path"] !== "string" ||
    typeof database["migration_revision"] !== "string" ||
    typeof database["journal_mode"] !== "string" ||
    typeof database["synchronous"] !== "string" ||
    typeof database["foreign_keys"] !== "boolean"
  ) {
    return undefined;
  }

  return {
    status: "ok",
    message: value["message"] as string,
    data: {
      system: {
        status: system["status"] as string,
        version: system["version"] as string,
        api_version: system["api_version"] as string,
        address: system["address"] as string,
        database: {
          path: database["path"] as string,
          migration_revision: database["migration_revision"] as string,
          journal_mode: database["journal_mode"] as string,
          synchronous: database["synchronous"] as string,
          foreign_keys: database["foreign_keys"] as boolean,
        },
      },
    },
  };
}


function parseErrorEnvelope(value: unknown): { message: string; code?: string } | undefined {
  if (!isRecord(value)) {
    return undefined;
  }

  if (value["status"] !== "error" || typeof value["message"] !== "string") {
    return undefined;
  }

  let code: string | undefined;
  if (isRecord(value["data"]) && typeof value["data"]["code"] === "string") {
    code = value["data"]["code"] as string;
  }

  return { message: value["message"] as string, code };
}


function formatCoreError(message: string, code?: string): string {
  return code ? `Core error: ${message} (code: ${code})` : `Core error: ${message}`;
}


function formatText(envelope: StatusEnvelope): string {
  const system = envelope.data.system;
  const database = system.database;

  return [
    `status: ${system.status}`,
    `version: ${system.version}`,
    `api_version: ${system.api_version}`,
    `address: ${system.address}`,
    `database_path: ${database.path}`,
    `migration_revision: ${database.migration_revision}`,
    `journal_mode: ${database.journal_mode}`,
    `synchronous: ${database.synchronous}`,
    `foreign_keys: ${String(database.foreign_keys)}`,
  ].join("\n");
}


export async function runStatus(
  options: StatusOptions,
  deps: StatusDeps = {},
): Promise<void> {
  const fetchFn = deps.fetchFn ?? fetch;
  const stdout = deps.stdout ?? console.log;

  let response: Response;

  try {
    response = await fetchFn(STATUS_URL, { signal: AbortSignal.timeout(3000) });
  } catch {
    throw new Error(
      `Cannot reach crucible-core at ${STATUS_URL}. Start it with \`crucible serve\`.`,
    );
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }

  if (!response.ok) {
    const error = parseErrorEnvelope(body);
    if (error) {
      throw new Error(formatCoreError(error.message, error.code));
    }

    throw new Error(`Core error: request failed with status ${response.status}.`);
  }

  const envelope = parseSuccessEnvelope(body);
  if (!envelope) {
    const error = parseErrorEnvelope(body);
    if (error) {
      throw new Error(formatCoreError(error.message, error.code));
    }

    throw new Error("Invalid status response from crucible-core.");
  }

  if (options.json) {
    stdout(JSON.stringify(body, null, 2));
  } else {
    stdout(formatText(envelope));
  }
}
