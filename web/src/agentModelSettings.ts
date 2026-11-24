import { getPrefs, patchPrefs } from "./uiPrefs";

export const DEFAULT_AGENT_MODEL = "Claude 5.0 Sonnet";

export function getAgentModel(): string {
  return getPrefs().agentModel || DEFAULT_AGENT_MODEL;
}

export function setAgentModel(name: string): void {
  const value = (name || "").trim() || DEFAULT_AGENT_MODEL;
  patchPrefs({ agentModel: value });
}
