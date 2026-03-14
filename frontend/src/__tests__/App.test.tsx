import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

window.HTMLElement.prototype.scrollIntoView = vi.fn();

vi.mock("socket.io-client", () => {
  const emit = vi.fn();
  const on = vi.fn();
  const off = vi.fn();
  const connect = vi.fn();
  const disconnect = vi.fn();
  const socket = { emit, on, off, connect, disconnect, connected: false };
  return {
    io: vi.fn(() => socket),
  };
});

vi.mock("bpmn-auto-layout", () => ({
  layoutProcess: vi.fn().mockResolvedValue("<xml>mock</xml>"),
}));

vi.mock("bpmn-js/lib/NavigatedViewer", () => ({
  default: vi.fn().mockImplementation(() => ({
    importXML: vi.fn().mockResolvedValue(undefined),
    saveSVG: vi.fn().mockResolvedValue({ svg: "<svg></svg>" }),
    get: vi.fn().mockReturnValue({ zoom: vi.fn() }),
    destroy: vi.fn(),
  })),
}));

import App from "../App";

describe("App Component", () => {
  test("renders BPMN Generator title", () => {
    render(<App />);
    expect(screen.getByText("BPMN Generator")).toBeInTheDocument();
  });

  test("renders new session button", () => {
    render(<App />);
    expect(screen.getByText("+ New Session")).toBeInTheDocument();
  });

  test("renders empty chat message", () => {
    render(<App />);
    expect(screen.getByText("BPMN Diagram Generator")).toBeInTheDocument();
  });

  test("renders input placeholder for new session", () => {
    render(<App />);
    expect(
      screen.getByPlaceholderText("Describe a business process...")
    ).toBeInTheDocument();
  });

  test("renders send button", () => {
    render(<App />);
    expect(screen.getByText("Send")).toBeInTheDocument();
  });

  test("send button is disabled when input is empty", () => {
    render(<App />);
    const sendButton = screen.getByText("Send");
    expect(sendButton).toBeDisabled();
  });
});
