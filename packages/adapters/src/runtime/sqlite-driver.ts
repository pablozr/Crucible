export type SqliteValue = string | number | bigint | Uint8Array | null;

export type SqliteRunResult = {
  changes: number;
};

export type SqliteStatement = {
  run: (...parameters: SqliteValue[]) => SqliteRunResult;
  get: (...parameters: SqliteValue[]) => unknown;
  all: (...parameters: SqliteValue[]) => unknown[];
};

export type SqliteDatabase = {
  exec: (sql: string) => void;
  prepare: (sql: string) => SqliteStatement;
  close: () => void;
};

export type SqliteDriverName = "node-sqlite" | "bun-sqlite";

export type SqliteDriver = {
  name: SqliteDriverName;
  open: (path: string) => SqliteDatabase;
};

export const SQLITE_DRIVER_UNAVAILABLE = "TERMINAL_OUTBOX_SQLITE_UNAVAILABLE";

// Loose structural view shared by node:sqlite and bun:sqlite. Both expose
// exec/prepare/close with run/get/all statements; concrete runtime types are
// cast through unknown because their parameter unions are not mutually
// assignable and no shared type package exists for bun:sqlite.
type RawStatement = {
  run: (...parameters: unknown[]) => unknown;
  get: (...parameters: unknown[]) => unknown;
  all: (...parameters: unknown[]) => unknown;
};

type RawDatabase = {
  exec: (sql: string) => unknown;
  prepare: (sql: string) => RawStatement;
  close: () => unknown;
};

type RawDatabaseConstructor = new (
  path: string,
  options?: { create?: boolean },
) => object;

function toRunResult(result: unknown): SqliteRunResult {
  const changes = (result as { changes?: number | bigint } | null | undefined)
    ?.changes;
  if (typeof changes === "bigint") return { changes: Number(changes) };
  return { changes: typeof changes === "number" ? changes : 0 };
}

function toFirstRow(result: unknown): unknown {
  // bun:sqlite reports an empty get() as null while node:sqlite uses undefined.
  return result ?? undefined;
}

function wrapDatabase(database: RawDatabase): SqliteDatabase {
  return {
    exec: (sql) => {
      database.exec(sql);
    },
    prepare: (sql) => {
      const statement = database.prepare(sql);
      return {
        run: (...parameters: SqliteValue[]) =>
          toRunResult(statement.run(...parameters)),
        get: (...parameters: SqliteValue[]) =>
          toFirstRow(statement.get(...parameters)),
        all: (...parameters: SqliteValue[]) =>
          statement.all(...parameters) as unknown[],
      };
    },
    close: () => {
      database.close();
    },
  };
}

let driver: Promise<SqliteDriver> | undefined;

// node:sqlite is preferred on Node >= 22.5; OpenCode's embedded Bun runtime
// (1.3.14) rejects that module and must fall back to bun:sqlite. Both loads
// stay dynamic so an unsupported module can never prevent this adapter module
// from being imported.
export function loadSqliteDriver(): Promise<SqliteDriver> {
  driver ??= (async () => {
    try {
      const nodeSqlite = await import("node:sqlite");
      if (nodeSqlite.DatabaseSync) {
        return {
          name: "node-sqlite",
          open: (path) =>
            wrapDatabase(new nodeSqlite.DatabaseSync(path) as unknown as RawDatabase),
        } satisfies SqliteDriver;
      }
    } catch {
      // Absent outside Node; the Bun probe below decides instead.
    }

    try {
      const bunSpecifier: string = "bun:sqlite";
      const bunModule = (await import(bunSpecifier)) as {
        Database?: RawDatabaseConstructor;
      };
      if (bunModule.Database) {
        const BunDatabase = bunModule.Database;
        return {
          name: "bun-sqlite",
          open: (path) =>
            wrapDatabase(new BunDatabase(path, { create: true }) as unknown as RawDatabase),
        } satisfies SqliteDriver;
      }
    } catch {
      // Neither driver exists in this runtime.
    }

    throw new Error(SQLITE_DRIVER_UNAVAILABLE);
  })();
  return driver;
}
