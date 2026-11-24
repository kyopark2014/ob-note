import { getPrefs, patchPrefs } from "./uiPrefs";

export const AGENT_W_DEFAULT = 360;
export const AGENT_W_MIN = 280;
export const AGENT_W_MAX = 640;

export function getAgentWidth(): number {
  return getPrefs().agentWidth;
}

export function setAgentWidth(width: number): void {
  patchPrefs({ agentWidth: clampAgentWidth(width) });
}

export function clampAgentWidth(width: number): number {
  return Math.min(AGENT_W_MAX, Math.max(AGENT_W_MIN, Math.round(width)));
}
