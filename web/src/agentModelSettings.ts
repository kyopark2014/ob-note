const AGENT_MODEL_KEY = "ob-note:agent-model";

export const DEFAULT_AGENT_MODEL = "Claude 5.0 Sonnet";

export function getAgentModel(): string {
  try {
    const v = localStorage.getItem(AGENT_MODEL_KEY);
    if (v && v.trim()) return v.trim();
  } catch {
    /* ignore */
  }
  return DEFAULT_AGENT_MODEL;
}

export function setAgentModel(name: string): void {
  const value = (name || "").trim() || DEFAULT_AGENT_MODEL;
  try {
    localStorage.setItem(AGENT_MODEL_KEY, value);
  } catch {
    /* ignore */
  }
}
