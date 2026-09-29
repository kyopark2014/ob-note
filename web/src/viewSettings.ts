const SHOW_IMAGES_KEY = "ob-note:view-show-images";

const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|ico|bmp|heic|avif)$/i;
const VIDEO_EXT = /\.(mp4|m4v|webm)$/i;

export function isImageFileName(name: string): boolean {
  return IMAGE_EXT.test(name);
}

export function isVideoFileName(name: string): boolean {
  return VIDEO_EXT.test(name.split("?")[0].split("#")[0]);
}

/** Images and videos sit beside notes and follow the Images view toggle. */
export function isCompanionMediaFileName(name: string): boolean {
  return isImageFileName(name) || isVideoFileName(name);
}

export function getShowImages(): boolean {
  try {
    return localStorage.getItem(SHOW_IMAGES_KEY) === "1";
  } catch {
    return false;
  }
}

export function setShowImages(value: boolean): void {
  try {
    localStorage.setItem(SHOW_IMAGES_KEY, value ? "1" : "0");
  } catch {
    /* ignore */
  }
}
