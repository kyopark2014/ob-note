import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type DragEvent,
  type TouchEvent,
} from "react";
import type { TreeNode } from "../types";
import { isFolderOpen, setFolderOpen } from "../treeSettings";
import { ChevronIcon, PinIcon } from "./Icons";

const DND_TYPE = "application/x-ob-note-path";
const DND_PREFIX = "ob-note-move|";
/** Long-press duration for mobile context menu (ms). */
const LONG_PRESS_MS = 480;
const LONG_PRESS_MOVE_PX = 12;

type DropHighlight =
  | { mode: "folder"; path: string }
  | { mode: "insert"; path: string; place: "before" | "after" }
  | null;

type Dragging = { path: string; kind: "file" | "folder" } | null;

const DropHighlightCtx = createContext<{
  highlight: DropHighlight;
  setHighlight: (h: DropHighlight) => void;
  dragging: Dragging;
  setDragging: (d: Dragging) => void;
  clear: () => void;
}>({
  highlight: null,
  setHighlight: () => {},
  dragging: null,
  setDragging: () => {},
  clear: () => {},
});

export type DraftFolder = {
  parentPath: string; // "" = vault root
};

type DragPayload = {
  path: string;
  kind: "file" | "folder";
};

export function serializeVaultMove(path: string, kind: "file" | "folder" = "file"): string {
  return `${DND_PREFIX}${kind}|${path}`;
}

/** Attach vault move payload so drops on folders call onMove (notes + images). */
export function setVaultMoveDataTransfer(
  dt: DataTransfer,
  path: string,
  kind: "file" | "folder" = "file",
): void {
  const serialized = serializeVaultMove(path, kind);
  dt.setData(DND_TYPE, serialized);
  dt.setData("text/plain", serialized);
  // copyMove: tree/folder move + Agent chat attach (copy)
  dt.effectAllowed = "copyMove";
}

type Props = {
  nodes: TreeNode[];
  activePath: string | null;
  selectedFolder?: string | null;
  /** Which row kind shows the selection highlight. */
  treeFocus?: "file" | "folder";
  onOpen: (path: string) => void;
  onSelectFolder?: (path: string) => void;
  onFolderContextMenu?: (path: string, x: number, y: number) => void;
  onFileContextMenu?: (path: string, x: number, y: number) => void;
  /** Empty area of the tree (vault root create menu). */
  onPanelContextMenu?: (x: number, y: number) => void;
  onMove?: (fromPath: string, toParentPath: string) => void;
  /** Same-folder custom order (basename list including folders + files). */
  onReorder?: (folderPath: string, names: string[]) => void;
  onUploadFiles?: (parentPath: string, files: File[]) => void;
  draftFolder?: DraftFolder | null;
  onDraftConfirm?: (name: string) => void;
  onDraftCancel?: () => void;
  renamingPath?: string | null;
  onRenameConfirm?: (path: string, name: string) => void;
  onRenameCancel?: () => void;
  depth?: number;
  forceOpenPaths?: Set<string>;
  pinnedPaths?: Set<string>;
  hidePinBadge?: boolean;
};

export function parseVaultDrag(e: DragEvent): DragPayload | null {
  try {
    const raw = e.dataTransfer.getData(DND_TYPE) || e.dataTransfer.getData("text/plain");
    if (!raw) return null;
    if (raw.startsWith(DND_PREFIX)) {
      const rest = raw.slice(DND_PREFIX.length);
      const bar = rest.indexOf("|");
      if (bar < 0) return null;
      const kind = rest.slice(0, bar);
      const path = rest.slice(bar + 1);
      if (!path || (kind !== "file" && kind !== "folder")) return null;
      return { path, kind };
    }
    const data = JSON.parse(raw) as DragPayload;
    if (!data?.path || (data.kind !== "file" && data.kind !== "folder")) return null;
    return data;
  } catch {
    return null;
  }
}

function parseDrag(e: DragEvent): DragPayload | null {
  return parseVaultDrag(e);
}

function hasExternalFiles(e: DragEvent): boolean {
  return Array.from(e.dataTransfer.types || []).includes("Files");
}

function isInternalMoveDrag(e: DragEvent): boolean {
  const types = Array.from(e.dataTransfer.types || []);
  return types.includes(DND_TYPE);
}

function parentDir(path: string): string {
  return path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
}

function canDropOnFolder(fromPath: string, fromKind: "file" | "folder", toFolder: string): boolean {
  if (fromPath === toFolder) return false;
  if (fromKind === "folder" && (toFolder === fromPath || toFolder.startsWith(fromPath + "/"))) {
    return false;
  }
  if (parentDir(fromPath) === toFolder) return false;
  return true;
}

export function computeReorderNames(
  siblings: TreeNode[],
  fromPath: string,
  targetPath: string,
  place: "before" | "after",
): string[] | null {
  if (parentDir(fromPath) !== parentDir(targetPath)) return null;
  const fromName = fromPath.split("/").pop() || fromPath;
  const targetName = targetPath.split("/").pop() || targetPath;
  if (fromName === targetName) return null;
  const names = siblings.map((s) => s.name);
  if (!names.includes(fromName) || !names.includes(targetName)) return null;
  const without = names.filter((n) => n !== fromName);
  let idx = without.indexOf(targetName);
  if (idx < 0) return null;
  if (place === "after") idx += 1;
  without.splice(idx, 0, fromName);
  return without;
}

function insertPlaceFromEvent(
  e: DragEvent,
  el: HTMLElement,
  isFolder: boolean,
): "before" | "after" | "into" {
  const rect = el.getBoundingClientRect();
  const ratio = (e.clientY - rect.top) / Math.max(rect.height, 1);
  if (isFolder) {
    if (ratio < 0.28) return "before";
    if (ratio > 0.72) return "after";
    return "into";
  }
  return ratio < 0.5 ? "before" : "after";
}

/** Prefer vault move over OS-file upload when both are present. */
export function acceptDrop(
  e: DragEvent,
  folderPath: string,
  onMove?: (fromPath: string, toParentPath: string) => void,
  onUploadFiles?: (parentPath: string, files: File[]) => void,
): boolean {
  const data = parseDrag(e);
  if (data && onMove) {
    if (!canDropOnFolder(data.path, data.kind, folderPath)) return false;
    onMove(data.path, folderPath);
    return true;
  }
  if (onUploadFiles && e.dataTransfer.files?.length) {
    onUploadFiles(folderPath, Array.from(e.dataTransfer.files));
    return true;
  }
  return false;
}

export function isVaultMoveDrag(e: DragEvent): boolean {
  return isInternalMoveDrag(e) || Array.from(e.dataTransfer.types || []).includes("text/plain");
}

export function isMarkdownNotePath(path: string): boolean {
  return /\.(md|markdown)$/i.test(path);
}

let activeVaultDrag: DragPayload | null = null;

export function setActiveVaultDrag(payload: DragPayload | null): void {
  activeVaultDrag = payload;
}

export function getActiveVaultDrag(): DragPayload | null {
  return activeVaultDrag;
}

/** Markdown notes copy into another folder. Same-folder reorder stays a move. */
export function vaultDropEffect(targetFolder: string): "copy" | "move" {
  const drag = activeVaultDrag;
  if (!drag || drag.kind !== "file" || !isMarkdownNotePath(drag.path)) return "move";
  if (parentDir(drag.path) === targetFolder) return "move";
  return "copy";
}

export function hasExternalFileDrag(e: DragEvent): boolean {
  return hasExternalFiles(e);
}

/** Clear editor selection/focus so tree HTML5 drag is not stolen by textarea text drag. */
function prepareTreeRowDrag(e: { button: number }) {
  if (e.button !== 0) return;
  try {
    window.getSelection()?.removeAllRanges();
  } catch {
    /* ignore */
  }
  const active = document.activeElement;
  if (
    active instanceof HTMLElement &&
    (active.tagName === "TEXTAREA" ||
      active.tagName === "INPUT" ||
      active.isContentEditable)
  ) {
    active.blur();
  }
}

/** True only for fine pointers (mouse); HTML5 drag steals long-press on touch. */
function useNativeDragEnabled(wantDrag: boolean): boolean {
  const [ok, setOk] = useState(() =>
    wantDrag && typeof window !== "undefined"
      ? window.matchMedia("(pointer: fine)").matches
      : false,
  );
  useEffect(() => {
    if (!wantDrag) {
      setOk(false);
      return;
    }
    const mq = window.matchMedia("(pointer: fine)");
    const sync = () => setOk(mq.matches);
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, [wantDrag]);
  return ok;
}

/** Touch long-press → context menu (mobile has no right-click). */
function useLongPressContextMenu(
  onOpen: ((x: number, y: number) => void) | undefined,
  suppressClick: { current: boolean },
) {
  const timerRef = useRef<number | null>(null);
  const startRef = useRef<{ x: number; y: number } | null>(null);
  const firedRef = useRef(false);

  const clearTimer = useCallback(() => {
    if (timerRef.current != null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    startRef.current = null;
  }, []);

  useEffect(() => () => clearTimer(), [clearTimer]);

  const onTouchStart = useCallback(
    (e: TouchEvent) => {
      if (!onOpen || e.touches.length !== 1) return;
      const t = e.touches[0];
      startRef.current = { x: t.clientX, y: t.clientY };
      firedRef.current = false;
      clearTimer();
      timerRef.current = window.setTimeout(() => {
        timerRef.current = null;
        const pos = startRef.current;
        startRef.current = null;
        if (!pos) return;
        firedRef.current = true;
        suppressClick.current = true;
        try {
          navigator.vibrate?.(12);
        } catch {
          /* ignore */
        }
        onOpen(pos.x, pos.y);
        window.setTimeout(() => {
          suppressClick.current = false;
        }, 500);
      }, LONG_PRESS_MS);
    },
    [clearTimer, onOpen, suppressClick],
  );

  const onTouchMove = useCallback(
    (e: TouchEvent) => {
      const start = startRef.current;
      if (!start || e.touches.length !== 1) return;
      const t = e.touches[0];
      const dx = t.clientX - start.x;
      const dy = t.clientY - start.y;
      if (dx * dx + dy * dy > LONG_PRESS_MOVE_PX * LONG_PRESS_MOVE_PX) {
        clearTimer();
      }
    },
    [clearTimer],
  );

  const onTouchEnd = useCallback(
    (e: TouchEvent) => {
      // Suppress the synthetic click/mousedown that would instantly dismiss the menu.
      if (firedRef.current) {
        e.preventDefault();
        e.stopPropagation();
        firedRef.current = false;
      }
      clearTimer();
    },
    [clearTimer],
  );

  if (!onOpen) return {};
  return {
    onTouchStart,
    onTouchMove,
    onTouchEnd,
    onTouchCancel: onTouchEnd,
  };
}

export function FileTree(props: Props) {
  const { depth = 0 } = props;

  if (depth === 0) {
    return <FileTreeRoot {...props} />;
  }
  return <FileTreeBranch {...props} />;
}

function FileTreeRoot(props: Props) {
  const [highlight, setHighlight] = useState<DropHighlight>(null);
  const [dragging, setDragging] = useState<Dragging>(null);
  const clear = useCallback(() => {
    setHighlight(null);
    setDragging(null);
  }, []);

  useEffect(() => {
    function onDragEnd() {
      setActiveVaultDrag(null);
      clear();
    }
    window.addEventListener("dragend", onDragEnd);
    return () => {
      window.removeEventListener("dragend", onDragEnd);
    };
  }, [clear]);

  return (
    <DropHighlightCtx.Provider value={{ highlight, setHighlight, dragging, setDragging, clear }}>
      <FileTreeBranch {...props} depth={0} />
    </DropHighlightCtx.Provider>
  );
}

function FileTreeBranch(props: Props) {
  const {
    nodes,
    draftFolder = null,
    onDraftConfirm,
    onDraftCancel,
    onMove,
    onUploadFiles,
    onPanelContextMenu,
    depth = 0,
  } = props;
  const { highlight, setHighlight, clear } = useContext(DropHighlightCtx);

  const showDraftHere =
    !!draftFolder && depth === 0 && draftFolder.parentPath === "";

  const rootDropActive =
    highlight?.mode === "folder" && highlight.path === "";

  return (
    <div
      className={
        depth === 0
          ? `file-tree${rootDropActive ? " drop-target" : ""}`
          : undefined
      }
      onContextMenu={
        depth === 0 && onPanelContextMenu
          ? (e) => {
              const el = e.target as HTMLElement;
              if (el.closest?.(".tree-item")) return;
              e.preventDefault();
              onPanelContextMenu(e.clientX, e.clientY);
            }
          : undefined
      }
      onDragOver={
        depth === 0 && (onMove || onUploadFiles)
          ? (e) => {
              const el = e.target as HTMLElement;
              const onChildItem = !!el.closest?.(".tree-item");
              if (isInternalMoveDrag(e) || hasExternalFiles(e) || e.dataTransfer.types.includes("text/plain")) {
                e.preventDefault();
                e.dataTransfer.dropEffect =
                  hasExternalFiles(e) && !isInternalMoveDrag(e) ? "copy" : vaultDropEffect("");
              }
              if (!onChildItem) setHighlight({ mode: "folder", path: "" });
            }
          : undefined
      }
      onDragLeave={
        depth === 0
          ? (e) => {
              if (e.currentTarget.contains(e.relatedTarget as Node)) return;
              clear();
            }
          : undefined
      }
      onDrop={
        depth === 0 && (onMove || onUploadFiles)
          ? (e) => {
              e.preventDefault();
              e.stopPropagation();
              clear();
              const el = e.target as HTMLElement;
              if (el.closest?.(".tree-item")) return;
              acceptDrop(e, "", onMove, onUploadFiles);
            }
          : undefined
      }
    >
      {nodes.map((node) => (
        <TreeRow
          key={node.path}
          node={node}
          siblings={nodes}
          {...props}
          draftFolder={draftFolder}
          depth={depth}
        />
      ))}
      {showDraftHere && onDraftConfirm && onDraftCancel && (
        <DraftFolderRow
          depth={depth}
          onConfirm={onDraftConfirm}
          onCancel={onDraftCancel}
        />
      )}
    </div>
  );
}

function TreeRow({
  node,
  siblings,
  activePath,
  selectedFolder = null,
  treeFocus = "file",
  onOpen,
  onSelectFolder,
  onFolderContextMenu,
  onFileContextMenu,
  onMove,
  onReorder,
  onUploadFiles,
  draftFolder = null,
  onDraftConfirm,
  onDraftCancel,
  renamingPath = null,
  onRenameConfirm,
  onRenameCancel,
  depth = 0,
  forceOpenPaths,
  pinnedPaths,
  hidePinBadge = false,
}: Props & { node: TreeNode; siblings: TreeNode[] }) {
  const { highlight, setHighlight, dragging, setDragging, clear } = useContext(DropHighlightCtx);
  const suppressClick = useRef(false);
  const nativeDrag = useNativeDragEnabled(!!(onMove || onReorder));
  const longPress = useLongPressContextMenu(
    node.type === "folder"
      ? onFolderContextMenu
        ? (x, y) => {
            onSelectFolder?.(node.path);
            onFolderContextMenu(node.path, x, y);
          }
        : undefined
      : onFileContextMenu
        ? (x, y) => {
            onFileContextMenu(node.path, x, y);
          }
        : undefined,
    suppressClick,
  );
  const shouldForce =
    forceOpenPaths?.has(node.path) ||
    draftFolder?.parentPath === node.path ||
    (draftFolder?.parentPath.startsWith(node.path + "/") ?? false) ||
    (activePath?.startsWith(node.path + "/") ?? false) ||
    (renamingPath?.startsWith(node.path + "/") ?? false);

  const [open, setOpen] = useState(
    () => isFolderOpen(node.path) || !!shouldForce,
  );

  useEffect(() => {
    if (!shouldForce) return;
    setOpen(true);
    setFolderOpen(node.path, true);
  }, [shouldForce, node.path]);

  const toggleOpen = useCallback(() => {
    setOpen((v) => {
      const next = !v;
      setFolderOpen(node.path, next);
      return next;
    });
  }, [node.path]);

  const canDnD = !!(onMove || onReorder || onUploadFiles);
  const folderPath = parentDir(node.path);
  const isInsertBefore =
    highlight?.mode === "insert" &&
    highlight.path === node.path &&
    highlight.place === "before";
  const isInsertAfter =
    highlight?.mode === "insert" &&
    highlight.path === node.path &&
    highlight.place === "after";
  const isFolderDrop =
    highlight?.mode === "folder" && highlight.path === node.path;

  const handleSiblingDragOver = (e: DragEvent<HTMLDivElement>, isFolder: boolean) => {
    if (!canDnD) return;
    e.preventDefault();
    e.stopPropagation();
    const dragPath = dragging?.path;
    const sameFolder =
      !!dragPath && parentDir(dragPath) === folderPath && dragPath !== node.path;
    const place = insertPlaceFromEvent(e, e.currentTarget, isFolder);
    const intoFolder = isFolder && (place === "into" || !sameFolder);
    const targetFolder = intoFolder ? node.path : folderPath;
    e.dataTransfer.dropEffect =
      hasExternalFiles(e) && !isInternalMoveDrag(e)
        ? "copy"
        : vaultDropEffect(targetFolder);

    if (sameFolder && onReorder && (place === "before" || place === "after")) {
      setHighlight({ mode: "insert", path: node.path, place });
      return;
    }
    if (isFolder && place === "into") {
      setHighlight({ mode: "folder", path: node.path });
      return;
    }
    if (sameFolder && onReorder) {
      setHighlight({
        mode: "insert",
        path: node.path,
        place: place === "into" ? "after" : place,
      });
      return;
    }
    // Cross-folder: file row → move into parent; folder row → into folder
    if (isFolder) {
      setHighlight({ mode: "folder", path: node.path });
    } else {
      setHighlight({ mode: "folder", path: folderPath });
    }
  };

  const handleSiblingDrop = (e: DragEvent<HTMLDivElement>, isFolder: boolean) => {
    e.preventDefault();
    e.stopPropagation();
    const place = insertPlaceFromEvent(e, e.currentTarget, isFolder);
    const data = parseDrag(e);
    clear();
    if (!data) {
      if (isFolder) acceptDrop(e, node.path, onMove, onUploadFiles);
      else acceptDrop(e, folderPath, onMove, onUploadFiles);
      return;
    }
    const sameFolder = parentDir(data.path) === folderPath && data.path !== node.path;
    if (sameFolder && onReorder && (place === "before" || place === "after")) {
      const names = computeReorderNames(siblings, data.path, node.path, place);
      if (names) {
        onReorder(folderPath, names);
        return;
      }
    }
    if (isFolder && (place === "into" || !sameFolder)) {
      if (acceptDrop(e, node.path, onMove, onUploadFiles)) {
        setOpen(true);
        setFolderOpen(node.path, true);
      }
      return;
    }
    if (sameFolder && onReorder) {
      const names = computeReorderNames(
        siblings,
        data.path,
        node.path,
        place === "into" ? "after" : place,
      );
      if (names) {
        onReorder(folderPath, names);
        return;
      }
    }
    acceptDrop(e, isFolder ? node.path : folderPath, onMove, onUploadFiles);
  };

  if (node.type === "folder") {
    const showDraftInside =
      !!draftFolder &&
      draftFolder.parentPath === node.path &&
      open &&
      onDraftConfirm &&
      onDraftCancel;

    const isSelected = treeFocus === "folder" && selectedFolder === node.path;
    const isRenaming = renamingPath === node.path;

    return (
      <div className="tree-folder">
        {isRenaming && onRenameConfirm && onRenameCancel ? (
          <RenameRow
            depth={depth}
            initial={node.name}
            isFolder
            onConfirm={(name) => onRenameConfirm(node.path, name)}
            onCancel={onRenameCancel}
          />
        ) : (
          <div
            className={`tree-item${isSelected ? " selected" : ""}${isFolderDrop ? " drop-over" : ""}${isInsertBefore ? " drop-insert-before" : ""}${isInsertAfter ? " drop-insert-after" : ""}`}
            style={{ paddingLeft: 10 + depth * 14 }}
            draggable={nativeDrag}
            {...longPress}
            onMouseDown={prepareTreeRowDrag}
            onDragStart={(e) => {
              if (!onMove && !onReorder) return;
              prepareTreeRowDrag(e);
              suppressClick.current = true;
              const payload: DragPayload = { path: node.path, kind: "folder" };
              setVaultMoveDataTransfer(e.dataTransfer, payload.path, payload.kind);
              setActiveVaultDrag(payload);
              setDragging(payload);
              setHighlight(null);
            }}
            onDragEnd={() => {
              setActiveVaultDrag(null);
              clear();
              window.setTimeout(() => {
                suppressClick.current = false;
              }, 50);
            }}
            onDragOver={(e) => handleSiblingDragOver(e, true)}
            onDragLeave={(e) => {
              if (e.currentTarget.contains(e.relatedTarget as Node)) return;
              if (
                (highlight?.mode === "folder" && highlight.path === node.path) ||
                (highlight?.mode === "insert" && highlight.path === node.path)
              ) {
                setHighlight(null);
              }
            }}
            onDrop={(e) => handleSiblingDrop(e, true)}
            onClick={() => {
              if (suppressClick.current) return;
              onSelectFolder?.(node.path);
              toggleOpen();
            }}
            onContextMenu={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onSelectFolder?.(node.path);
              onFolderContextMenu?.(node.path, e.clientX, e.clientY);
            }}
          >
            <span className="tree-chevron">
              <ChevronIcon open={open} />
            </span>
            {!hidePinBadge && pinnedPaths?.has(node.path) && (
              <span className="tree-pin-icon" aria-hidden="true">
                <PinIcon />
              </span>
            )}
            <span className="tree-name">{node.name}</span>
          </div>
        )}
        {open && (
          <div
            className={`tree-children${isFolderDrop ? " drop-over" : ""}`}
            onDragOver={
              onMove || onUploadFiles
                ? (e) => {
                    const el = e.target as HTMLElement;
                    if (el.closest?.(".tree-item")) return;
                    e.preventDefault();
                    e.stopPropagation();
                    e.dataTransfer.dropEffect =
                      hasExternalFiles(e) && !isInternalMoveDrag(e)
                        ? "copy"
                        : vaultDropEffect(node.path);
                    setHighlight({ mode: "folder", path: node.path });
                  }
                : undefined
            }
            onDrop={
              onMove || onUploadFiles
                ? (e) => {
                    const el = e.target as HTMLElement;
                    if (el.closest?.(".tree-item")) return;
                    e.preventDefault();
                    e.stopPropagation();
                    clear();
                    acceptDrop(e, node.path, onMove, onUploadFiles);
                  }
                : undefined
            }
          >
            {node.children && (
              <FileTree
                nodes={node.children}
                activePath={activePath}
                selectedFolder={selectedFolder}
                treeFocus={treeFocus}
                onOpen={onOpen}
                onSelectFolder={onSelectFolder}
                onFolderContextMenu={onFolderContextMenu}
                onFileContextMenu={onFileContextMenu}
                onMove={onMove}
                onReorder={onReorder}
                onUploadFiles={onUploadFiles}
                draftFolder={draftFolder}
                onDraftConfirm={onDraftConfirm}
                onDraftCancel={onDraftCancel}
                renamingPath={renamingPath}
                onRenameConfirm={onRenameConfirm}
                onRenameCancel={onRenameCancel}
                depth={depth + 1}
                forceOpenPaths={forceOpenPaths}
                pinnedPaths={pinnedPaths}
                hidePinBadge={hidePinBadge}
              />
            )}
            {showDraftInside && (
              <DraftFolderRow
                depth={depth + 1}
                onConfirm={onDraftConfirm!}
                onCancel={onDraftCancel!}
              />
            )}
          </div>
        )}
      </div>
    );
  }

  const isRenaming = renamingPath === node.path;
  const displayName = node.name.replace(/\.md$/i, "");

  if (isRenaming && onRenameConfirm && onRenameCancel) {
    return (
      <RenameRow
        depth={depth}
        initial={displayName}
        onConfirm={(name) => onRenameConfirm(node.path, name)}
        onCancel={onRenameCancel}
      />
    );
  }

  return (
    <div
      className={`tree-item${treeFocus !== "folder" && activePath === node.path ? " active" : ""}${isInsertBefore ? " drop-insert-before" : ""}${isInsertAfter ? " drop-insert-after" : ""}`}
      style={{ paddingLeft: 10 + depth * 14 + 14 }}
      draggable={nativeDrag}
      {...longPress}
      onMouseDown={prepareTreeRowDrag}
      onDragStart={(e) => {
        if (!onMove && !onReorder) return;
        prepareTreeRowDrag(e);
        suppressClick.current = true;
        const payload: DragPayload = { path: node.path, kind: "file" };
        setVaultMoveDataTransfer(e.dataTransfer, payload.path, payload.kind);
        setActiveVaultDrag(payload);
        setDragging(payload);
        setHighlight(null);
      }}
      onDragEnd={() => {
        setActiveVaultDrag(null);
        clear();
        // Keep suppress a beat so the synthetic click after drag does not open the file.
        window.setTimeout(() => {
          suppressClick.current = false;
        }, 50);
      }}
      onDragOver={canDnD ? (e) => handleSiblingDragOver(e, false) : undefined}
      onDragLeave={
        canDnD
          ? (e) => {
              if (e.currentTarget.contains(e.relatedTarget as Node)) return;
              if (highlight?.mode === "insert" && highlight.path === node.path) {
                setHighlight(null);
              }
            }
          : undefined
      }
      onDrop={canDnD ? (e) => handleSiblingDrop(e, false) : undefined}
      onClick={() => {
        if (suppressClick.current) return;
        onOpen(node.path);
      }}
      onContextMenu={(e) => {
        e.preventDefault();
        e.stopPropagation();
        onFileContextMenu?.(node.path, e.clientX, e.clientY);
      }}
    >
      {!hidePinBadge && pinnedPaths?.has(node.path) && (
        <span className="tree-pin-icon" aria-hidden="true">
          <PinIcon />
        </span>
      )}
      <span className="tree-name">{displayName}</span>
    </div>
  );
}

function DraftFolderRow({
  depth,
  onConfirm,
  onCancel,
}: {
  depth: number;
  onConfirm: (name: string) => void;
  onCancel: () => void;
}) {
  return (
    <RenameRow
      depth={depth}
      initial="Untitled"
      isFolder
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  );
}

function RenameRow({
  depth,
  initial,
  isFolder,
  onConfirm,
  onCancel,
}: {
  depth: number;
  initial: string;
  isFolder?: boolean;
  onConfirm: (name: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initial);
  const inputRef = useRef<HTMLInputElement>(null);
  const doneRef = useRef(false);

  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.focus();
    el.select();
  }, []);

  const finish = (commit: boolean) => {
    if (doneRef.current) return;
    doneRef.current = true;
    const name = value.trim();
    if (commit && name) onConfirm(name);
    else onCancel();
  };

  return (
    <div
      className="tree-item tree-item-draft"
      style={{ paddingLeft: 10 + depth * 14 + (isFolder ? 0 : 14) }}
    >
      {isFolder && (
        <span className="tree-chevron">
          <ChevronIcon open={false} />
        </span>
      )}
      <input
        ref={inputRef}
        className="tree-draft-input"
        value={value}
        spellCheck={false}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            finish(true);
          } else if (e.key === "Escape") {
            e.preventDefault();
            finish(false);
          }
        }}
        onBlur={() => finish(true)}
      />
    </div>
  );
}
