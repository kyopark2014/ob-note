import { useEffect, useState } from "react";
import { api } from "../api";

type Props = {
  path: string;
};

export function VideoPreview({ path }: Props) {
  const [failed, setFailed] = useState(false);
  const src = api.rawUrl(path);
  const name = path.split("/").pop() || path;

  useEffect(() => {
    setFailed(false);
  }, [path]);

  return (
    <div className="image-preview-pane video-preview-pane">
      {failed ? (
        <div className="image-preview-error">
          동영상을 불러오지 못했습니다.
          <br />
          <span className="image-preview-name">{name}</span>
        </div>
      ) : (
        <video
          key={path}
          className="video-preview-player"
          src={src}
          controls
          playsInline
          preload="metadata"
          onError={() => setFailed(true)}
        />
      )}
    </div>
  );
}
