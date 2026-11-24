import { getPrefs, patchPrefs } from "./uiPrefs";

const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|ico|bmp|heic|avif)$/i;
const VIDEO_EXT = /\.(mp4|m4v|webm)$/i;
/** Office/PDF/zip attachments droppable into a note folder (+ public share). */
const DOCUMENT_EXT = /\.(pdf|ppt|pptx|xls|xlsx|zip|doc|docx)$/i;

export function isImageFileName(name: string): boolean {
  return IMAGE_EXT.test(name);
}

export function isVideoFileName(name: string): boolean {
  return VIDEO_EXT.test(name.split("?")[0].split("#")[0]);
}

export function isDocumentFileName(name: string): boolean {
  return DOCUMENT_EXT.test(name.split("?")[0].split("#")[0]);
}

/** Images and videos sit beside notes and follow the Images view toggle. */
export function isCompanionMediaFileName(name: string): boolean {
  return isImageFileName(name) || isVideoFileName(name);
}

export function getShowImages(): boolean {
  return getPrefs().showImages;
}

export function setShowImages(value: boolean): void {
  patchPrefs({ showImages: value });
}
