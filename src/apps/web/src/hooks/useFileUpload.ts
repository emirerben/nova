/**
 * Reusable file upload hook: get presigned URL → XHR upload → progress tracking.
 *
 * Extracted from template/page.tsx upload logic. Works with any presigned URL provider.
 */

import { useCallback, useRef, useState } from "react";

export interface UploadFile {
  file: File;
  id: string;
  progress: number; // 0-100
  error: string | null;
  gcsPath: string | null;
}

interface PresignedResult {
  upload_url: string;
  gcs_path: string;
}

interface UseFileUploadOptions {
  /** Function that returns a presigned URL for a given file. */
  getPresignedUrl: (file: File) => Promise<PresignedResult>;
  /** Called when all files finish uploading (success or error). */
  onComplete?: (files: UploadFile[]) => void;
  /** Maximum concurrent uploads. */
  concurrency?: number;
}

export function useFileUpload({ getPresignedUrl, onComplete, concurrency = 3 }: UseFileUploadOptions) {
  const [files, setFiles] = useState<UploadFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const updateFile = useCallback((id: string, update: Partial<UploadFile>) => {
    setFiles((prev) => prev.map((f) => (f.id === id ? { ...f, ...update } : f)));
  }, []);

  const addFiles = useCallback((newFiles: File[]) => {
    const entries: UploadFile[] = newFiles.map((file) => ({
      file,
      id: crypto.randomUUID(),
      progress: 0,
      error: null,
      gcsPath: null,
    }));
    setFiles((prev) => [...prev, ...entries]);
    return entries;
  }, []);

  const removeFile = useCallback((id: string) => {
    setFiles((prev) => prev.filter((f) => f.id !== id));
  }, []);

  const clearFiles = useCallback(() => {
    setFiles([]);
  }, []);

  const uploadOne = useCallback(
    async (entry: UploadFile): Promise<UploadFile> => {
      try {
        const { upload_url, gcs_path } = await getPresignedUrl(entry.file);

        await new Promise<void>((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open("PUT", upload_url);
          xhr.setRequestHeader("Content-Type", entry.file.type);

          xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
              const pct = Math.round((e.loaded / e.total) * 100);
              updateFile(entry.id, { progress: pct });
            }
          };

          xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) {
              resolve();
            } else {
              reject(new Error(`Upload failed: ${xhr.status}`));
            }
          };

          xhr.onerror = () => reject(new Error("Upload network error"));
          xhr.send(entry.file);
        });

        const updated = { ...entry, progress: 100, gcsPath: gcs_path };
        updateFile(entry.id, { progress: 100, gcsPath: gcs_path });
        return updated;
      } catch (err) {
        const errMsg = err instanceof Error ? err.message : "Upload failed";
        updateFile(entry.id, { error: errMsg });
        return { ...entry, error: errMsg };
      }
    },
    [getPresignedUrl, updateFile],
  );

  const startUpload = useCallback(
    async (entries?: UploadFile[]) => {
      const toUpload = entries ?? files.filter((f) => f.progress === 0 && !f.error);
      if (toUpload.length === 0) return;

      setUploading(true);
      abortRef.current = new AbortController();
      const results: UploadFile[] = [];

      // Process in batches of `concurrency`
      for (let i = 0; i < toUpload.length; i += concurrency) {
        const batch = toUpload.slice(i, i + concurrency);
        const batchResults = await Promise.all(batch.map(uploadOne));
        results.push(...batchResults);
      }

      setUploading(false);
      onComplete?.(results);
    },
    [files, concurrency, uploadOne, onComplete],
  );

  const abort = useCallback(() => {
    abortRef.current?.abort();
    setUploading(false);
  }, []);

  const successfulPaths = files
    .filter((f) => f.gcsPath !== null)
    .map((f) => f.gcsPath!);

  return {
    files,
    uploading,
    addFiles,
    removeFile,
    clearFiles,
    startUpload,
    abort,
    successfulPaths,
  };
}
