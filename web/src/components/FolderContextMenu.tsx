import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

export type FolderMenuAction =
  | "new-note"
  | "new-folder"
  | "duplicate"
  | "compress"
  | "share"
  | "pin"
  | "rename"
  | "delete";

export type FileMenuAction =
  | "open-tab"
  | "open-agent"
  | "duplicate"
  | "share"
  | "pin"
  | "rename"
  | "delete";

export type PanelMenuAction = "new-note" | "new-folder";

export type ContextMenuState = {
  kind: "folder" | "file" | "panel";
  /** Folder/file path, or parent folder for panel ("" = vault root). */
  path: string;
  x: number;
  y: number;
};

/** @deprecated use ContextMenuState */
export type FolderContextMenuState = ContextMenuState;

type Props = {
  menu: ContextMenuState;
  pinned?: boolean;
  onFolderAction: (action: FolderMenuAction, path: string) => void;
  onFileAction: (action: FileMenuAction, path: string) => void;
  onPanelAction: (action: PanelMenuAction, parentPath: string) => void;
  onClose: () => void;
};

type MenuItem = {
  action: FolderMenuAction | FileMenuAction | PanelMenuAction;
  label: string;
  danger?: boolean;
  sepBefore?: boolean;
};

function panelItems(): MenuItem[] {
  return [
    { action: "new-note", label: "New note" },
    { action: "new-folder", label: "New folder" },
  ];
}

function folderItems(pinned: boolean): MenuItem[] {
  return [
    { action: "new-note", label: "New note" },
    { action: "new-folder", label: "New folder" },
    { action: "duplicate", label: "Duplicate", sepBefore: true },
    { action: "compress", label: "Compress" },
    { action: "share", label: "Share public link", sepBefore: true },
    { action: "pin", label: pinned ? "Unpin" : "Pin", sepBefore: true },
    { action: "rename", label: "Rename..." },
    { action: "delete", label: "Delete" },
  ];
}

function fileItems(pinned: boolean, canShare: boolean): MenuItem[] {
  const items: MenuItem[] = [
    { action: "open-tab", label: "Open in new tab" },
    { action: "open-agent", label: "Open agent" },
    { action: "duplicate", label: "Duplicate", sepBefore: true },
  ];
  if (canShare) {
    items.push({ action: "share", label: "Share public link" });
  }
  items.push(
    { action: "pin", label: pinned ? "Unpin" : "Pin", sepBefore: true },
    { action: "rename", label: "Rename..." },
    { action: "delete", label: "Delete" },
  );
  return items;
}

export function FolderContextMenu({
  menu,
  pinned = false,
  onFolderAction,
  onFileAction,
  onPanelAction,
  onClose,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    // Delay outside-dismiss: after long-press, finger-up synthesizes pointer/mouse
    // events on the tree item and would close the menu immediately.
    let removeOutside: (() => void) | undefined;
    const attachTimer = window.setTimeout(() => {
      function onOutside(e: Event) {
        if (ref.current && !ref.current.contains(e.target as Node)) onClose();
      }
      window.addEventListener("pointerdown", onOutside, true);
      removeOutside = () => window.removeEventListener("pointerdown", onOutside, true);
    }, 450);
    window.addEventListener("keydown", onKey);
    return () => {
      window.clearTimeout(attachTimer);
      removeOutside?.();
      window.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const pad = 8;
  const approxH = menu.kind === "panel" ? 90 : menu.kind === "folder" ? 360 : 320;
  const approxW = 200;
  const left = Math.min(menu.x, window.innerWidth - approxW - pad);
  const top = Math.min(menu.y, window.innerHeight - approxH - pad);

  const items =
    menu.kind === "panel"
      ? panelItems()
      : menu.kind === "folder"
        ? folderItems(pinned)
        : fileItems(pinned, /\.md$/i.test(menu.path));

  return createPortal(
    <div
      ref={ref}
      className="ctx-menu"
      style={{ left, top }}
      role="menu"
      onContextMenu={(e) => e.preventDefault()}
    >
      {items.map((item) => (
        <div key={item.action}>
          {item.sepBefore && <div className="ctx-sep" />}
          <button
            type="button"
            className={`ctx-item${item.danger ? " danger" : ""}`}
            role="menuitem"
            onClick={() => {
              if (menu.kind === "panel") {
                onPanelAction(item.action as PanelMenuAction, menu.path);
              } else if (menu.kind === "folder") {
                onFolderAction(item.action as FolderMenuAction, menu.path);
              } else {
                onFileAction(item.action as FileMenuAction, menu.path);
              }
              onClose();
            }}
          >
            {item.label}
          </button>
        </div>
      ))}
    </div>,
    document.body,
  );
}
