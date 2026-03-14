import React, { useEffect, useRef, useState } from "react";
import BpmnJS from "bpmn-js/lib/NavigatedViewer";
import { layoutProcess } from "bpmn-auto-layout";

interface BpmnViewerProps {
  xml: string;
}

type CopyFeedback = "xml" | "image" | null;

function stripDiagramInfo(xml: string): string {
  return xml.replace(/<bpmndi:BPMNDiagram[\s\S]*?<\/bpmndi:BPMNDiagram>/gi, "");
}

const BpmnViewer: React.FC<BpmnViewerProps> = ({ xml }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<any>(null);
  const [layoutedXml, setLayoutedXml] = useState<string>("");
  const [copyFeedback, setCopyFeedback] = useState<CopyFeedback>(null);

  useEffect(() => {
    if (!xml) return;

    const doLayout = async () => {
      try {
        const stripped = stripDiagramInfo(xml);
        const result = await layoutProcess(stripped);
        setLayoutedXml(result);
      } catch (err) {
        console.warn("Auto-layout failed, using original XML:", err);
        setLayoutedXml(xml);
      }
    };

    doLayout();
  }, [xml]);

  useEffect(() => {
    if (!containerRef.current || !layoutedXml) return;

    if (!viewerRef.current) {
      viewerRef.current = new BpmnJS({
        container: containerRef.current,
      });
    }

    viewerRef.current
      .importXML(layoutedXml)
      .then(() => {
        const canvas = viewerRef.current.get("canvas");
        canvas.zoom("fit-viewport");
      })
      .catch((err: any) => {
        console.error("Failed to render BPMN:", err);
      });
  }, [layoutedXml]);

  useEffect(() => {
    return () => {
      if (viewerRef.current) {
        viewerRef.current.destroy();
        viewerRef.current = null;
      }
    };
  }, []);

  const showFeedback = (type: CopyFeedback) => {
    setCopyFeedback(type);
    setTimeout(() => setCopyFeedback(null), 2000);
  };

  /** Convert current diagram SVG to a canvas, then return the blob */
  const diagramToBlob = (): Promise<Blob> => {
    return new Promise(async (resolve, reject) => {
      if (!viewerRef.current) return reject(new Error("No viewer"));
      try {
        const { svg } = await viewerRef.current.saveSVG();
        const canvas = document.createElement("canvas");
        const ctx = canvas.getContext("2d");
        const img = new Image();
        img.onload = () => {
          canvas.width = img.width;
          canvas.height = img.height;
          ctx?.drawImage(img, 0, 0);
          canvas.toBlob((blob) => {
            if (blob) resolve(blob);
            else reject(new Error("Canvas toBlob returned null"));
          }, "image/png");
        };
        img.onerror = () => reject(new Error("Failed to load SVG as image"));
        img.src =
          "data:image/svg+xml;base64," +
          btoa(unescape(encodeURIComponent(svg)));
      } catch (err) {
        reject(err);
      }
    });
  };

  const handleExportPNG = async () => {
    try {
      const blob = await diagramToBlob();
      const link = document.createElement("a");
      link.download = "bpmn-diagram.png";
      link.href = URL.createObjectURL(blob);
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (err) {
      console.error("PNG export failed:", err);
    }
  };

  const handleExportXML = () => {
    const exportXml = layoutedXml || xml;
    const blob = new Blob([exportXml], { type: "application/xml" });
    const link = document.createElement("a");
    link.download = "bpmn-diagram.bpmn";
    link.href = URL.createObjectURL(blob);
    link.click();
    URL.revokeObjectURL(link.href);
  };

  const handleCopyXML = async () => {
    const exportXml = layoutedXml || xml;
    try {
      await navigator.clipboard.writeText(exportXml);
      showFeedback("xml");
    } catch (err) {
      console.error("Copy XML failed:", err);
    }
  };

  const handleCopyImage = async () => {
    try {
      const blob = await diagramToBlob();
      await navigator.clipboard.write([
        new ClipboardItem({ "image/png": blob }),
      ]);
      showFeedback("image");
    } catch (err) {
      console.error("Copy image failed:", err);
    }
  };

  return (
    <div className="bpmn-viewer-container">
      <div ref={containerRef} className="bpmn-canvas" />
      <div className="bpmn-toolbar">
        <div className="toolbar-group">
          <span className="toolbar-label">Copy</span>
          <button
            onClick={handleCopyXML}
            className="toolbar-btn"
            title="Copy XML to clipboard"
          >
            {copyFeedback === "xml" ? "\u2713 Copied" : "XML"}
          </button>
          <button
            onClick={handleCopyImage}
            className="toolbar-btn"
            title="Copy image to clipboard"
          >
            {copyFeedback === "image" ? "\u2713 Copied" : "Image"}
          </button>
        </div>
        <div className="toolbar-group">
          <span className="toolbar-label">Download</span>
          <button
            onClick={handleExportXML}
            className="toolbar-btn"
            title="Download BPMN XML file"
          >
            XML
          </button>
          <button
            onClick={handleExportPNG}
            className="toolbar-btn"
            title="Download PNG image"
          >
            PNG
          </button>
        </div>
      </div>
    </div>
  );
};

export default BpmnViewer;
