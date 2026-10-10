import { Minus, Plus, RotateCw } from "lucide-react";
import * as pdfjs from "pdfjs-dist";
import type { PDFDocumentProxy } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { ErrorNote, Spinner } from "@/components/ui/misc";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

/** `placeholder`: an image of the first page shown at full width until the PDF has loaded. */
export function PdfViewer({ url, placeholder, onLoaded }: { url: string; placeholder?: string; onLoaded?: (pages: number) => void }) {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [placeholderFailed, setPlaceholderFailed] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [scale, setScale] = useState(1.2);
  const scroller = useRef<HTMLDivElement>(null);
  const [rotation, setRotation] = useState(0);
  const loadedCallback = useRef(onLoaded);
  loadedCallback.current = onLoaded;

  useEffect(() => {
    let cancelled = false;
    let loadingTask: ReturnType<typeof pdfjs.getDocument> | null = null;
    setPdf(null);
    setError(null);
    (async () => {
      try {
        const response = await fetch(url, { credentials: "same-origin" });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.detail ?? `Could not load the document (${response.status})`);
        }
        const data = new Uint8Array(await response.arrayBuffer());
        loadingTask = pdfjs.getDocument({
          data,
          wasmUrl: "/pdfjs/wasm/",
          cMapUrl: "/pdfjs/cmaps/",
          cMapPacked: true,
          standardFontDataUrl: "/pdfjs/standard_fonts/",
          iccUrl: "/pdfjs/iccs/",
        });
        const loaded = await loadingTask.promise;
        if (cancelled) return;
        // Fit the first page to the available width.
        const first = await loaded.getPage(1);
        const width = first.getViewport({ scale: 1 }).width;
        const available = (scroller.current?.clientWidth ?? 800) - 40;
        setScale(Math.min(2, Math.max(0.5, Math.round((available / width) * 10) / 10)));
        setPdf(loaded);
        loadedCallback.current?.(loaded.numPages);
      } catch (err) {
        if (!cancelled) setError(err);
      }
    })();
    return () => {
      cancelled = true;
      void loadingTask?.destroy();
    };
  }, [url]);

  if (error) return <ErrorNote error={error} />;
  if (!pdf && placeholder && !placeholderFailed)
    return (
      <div ref={scroller} className="relative h-full w-full overflow-auto bg-muted/40 p-5">
        <img
          src={placeholder}
          alt=""
          className="mx-auto w-full max-w-3xl bg-white shadow-md"
          onError={() => setPlaceholderFailed(true)}
        />
        <div className="absolute left-1/2 top-3 flex -translate-x-1/2 items-center gap-2 rounded-full border bg-card/95 px-3 py-1 text-xs text-muted-foreground shadow-sm">
          <Spinner className="size-3.5" /> Loading full document…
        </div>
      </div>
    );
  if (!pdf)
    return (
      <div ref={scroller} className="flex h-96 w-full flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
        <Spinner className="size-6" />
        Loading document from secure storage…
      </div>
    );

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-1 border-b bg-card px-2 py-1.5 text-sm">
        <span className="px-2 text-muted-foreground">
          {pdf.numPages} page{pdf.numPages === 1 ? "" : "s"}
        </span>
        <div className="ml-auto flex items-center gap-1">
          <Button variant="ghost" size="icon-sm" onClick={() => setScale((s) => Math.max(0.5, s - 0.2))} title="Zoom out">
            <Minus />
          </Button>
          <span className="w-12 text-center tabular-nums text-muted-foreground">{Math.round(scale * 100)}%</span>
          <Button variant="ghost" size="icon-sm" onClick={() => setScale((s) => Math.min(3, s + 0.2))} title="Zoom in">
            <Plus />
          </Button>
          <Button variant="ghost" size="icon-sm" onClick={() => setRotation((r) => (r + 90) % 360)} title="Rotate">
            <RotateCw />
          </Button>
        </div>
      </div>
      <div className="flex-1 overflow-auto bg-muted/60 p-4">
        <div className="flex flex-col gap-4">
          {Array.from({ length: pdf.numPages }, (_, i) => (
            <PdfPage key={i} pdf={pdf} pageNumber={i + 1} scale={scale} rotation={rotation} />
          ))}
        </div>
      </div>
    </div>
  );
}

function PdfPage({
  pdf,
  pageNumber,
  scale,
  rotation,
}: {
  pdf: PDFDocumentProxy;
  pageNumber: number;
  scale: number;
  rotation: number;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const container = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(pageNumber <= 2);

  useEffect(() => {
    if (visible || !container.current) return;
    const observer = new IntersectionObserver(
      (entries) => entries.some((e) => e.isIntersecting) && setVisible(true),
      { rootMargin: "400px" },
    );
    observer.observe(container.current);
    return () => observer.disconnect();
  }, [visible]);

  useEffect(() => {
    if (!visible) return;
    let task: { cancel: () => void } | null = null;
    let cancelled = false;
    (async () => {
      const page = await pdf.getPage(pageNumber);
      if (cancelled || !canvas.current) return;
      const ratio = window.devicePixelRatio || 1;
      const viewport = page.getViewport({ scale, rotation: (page.rotate + rotation) % 360 });
      const el = canvas.current;
      el.width = Math.floor(viewport.width * ratio);
      el.height = Math.floor(viewport.height * ratio);
      el.style.width = `${Math.floor(viewport.width)}px`;
      el.style.height = `${Math.floor(viewport.height)}px`;
      const render = page.render({
        canvas: el,
        viewport,
        transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : undefined,
      });
      task = render;
      await render.promise.catch(() => undefined);
    })();
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [pdf, pageNumber, scale, rotation, visible]);

  return (
    <div ref={container} className="pdf-page min-h-40">
      <canvas ref={canvas} />
    </div>
  );
}
