/// <reference types="vite/client" />

declare module "bpmn-js/lib/NavigatedViewer" {
  export default class NavigatedViewer {
    constructor(options: { container: HTMLElement });
    importXML(xml: string): Promise<void>;
    saveSVG(): Promise<{ svg: string }>;
    get(name: string): any;
    destroy(): void;
  }
}

declare module "bpmn-auto-layout" {
  export function layoutProcess(xml: string): Promise<string>;
}
