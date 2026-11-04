// Typy odpovídají modelu backendu (backend/cmu_backend/model.py, schéma na /openapi.json).

export type Health = "problem" | "warning" | "unknown" | "stopped" | "ok";
export type ChainKind = "process" | "container";
export type ChainState = "running" | "degraded" | "broken" | "starting" | "stopping" | "stopped" | "unknown";
export type Severity = "info" | "warning" | "average" | "high" | "disaster";
export type LocationKind = "vm" | "docker-host" | "demo";

export interface SourceRef {
  id: string;
  type: string;
}

export interface LocationRef {
  id: string;
  name: string;
  address: string | null;
  environment: string | null;
  kind: LocationKind;
}

export interface Alert {
  source: string;
  severity: Severity;
  name: string;
  host: string | null;
  since: string | null;
  url: string | null;
}

export interface Metric {
  id: string;
  label: string;
  value: number | null;
  unit: string | null;
  source: string;
}

export interface ChainApp {
  id: string;
  name: string;
  position: number;
  state: string;
  ready: boolean | null;
  version: string | null;
  image: string | null;
  port: number | null;
  restarts: number | null;
  configPending: boolean | null;
  detail: Record<string, unknown>;
}

export interface Chain {
  id: string;
  localId: string;
  name: string;
  kind: ChainKind;
  source: SourceRef;
  location: LocationRef;
  state: ChainState;
  health: Health;
  desiredRunning: boolean | null;
  stale: boolean;
  lastSeen: string | null;
  apps: ChainApp[];
  alerts: Alert[];
  metrics: Metric[];
  capabilities: string[];
  busyOperation: string | null;
}

export interface Location {
  id: string;
  name: string;
  kind: LocationKind;
  source: SourceRef;
  address: string | null;
  environment: string | null;
  reachable: boolean;
  lastSeen: string | null;
  error: string | null;
  alerts: Alert[];
  chainCount: number;
}

export interface ComponentStatus {
  id: string;
  type: string;
  role: "source" | "integration";
  ok: boolean;
  message: string;
  lastSuccess: string | null;
  lastAttempt: string | null;
  details: Record<string, unknown>;
}

export interface OperationStep {
  target: string;
  status: "pending" | "running" | "succeeded" | "failed";
  message: string;
}

export interface Operation {
  id: string;
  kind: string;
  target: string;
  status: "running" | "succeeded" | "failed";
  message: string;
  steps: OperationStep[];
  startedAt: string | null;
  finishedAt: string | null;
}

export interface Summary {
  chains: number;
  byHealth: Record<Health, number>;
  byKind: Partial<Record<ChainKind, number>>;
  locations: number;
  unreachableLocations: number;
  generatedAt: string;
}

export interface Target {
  sourceId: string;
  sourceType: string;
  kind: ChainKind;
  location: string;
  locationId: string;
  environment: string | null;
  reachable: boolean;
}

export interface CatalogEntry {
  type: string;
  versions: string[];
}

export interface ProcessAppSpec {
  type: string;
  version: string;
  config?: Record<string, unknown>;
}

export interface ContainerAppSpec {
  name: string;
  image: string;
  port: number;
  env: Record<string, string>;
  publish: number | null;
  healthcheck: boolean;
}

export interface ChainCreate {
  sourceId: string;
  location: string;
  kind: ChainKind;
  name: string;
  start: boolean;
  processApps: ProcessAppSpec[];
  containerApps: ContainerAppSpec[];
}

export interface ChainValidation {
  valid: boolean;
  errors: string[];
  warnings: string[];
  preview: string | null;
  assignedPorts: number[];
}

export interface Me {
  role: "none" | "viewer" | "operator";
  authEnabled: boolean;
}
