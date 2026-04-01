import React, { useState, useEffect, useCallback } from "react";
import socket from "./socket";
import Sidebar from "./components/Sidebar";
import ChatArea from "./components/ChatArea";
import { SessionInfo, HistoryEntry, ServerEvent } from "./types";
import "./App.css";

const App: React.FC = () => {
  const [userId, setUserId] = useState<string | null>(
    localStorage.getItem("bpmn_user_id")
  );
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [currentHistory, setCurrentHistory] = useState<HistoryEntry[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [sessionName, setSessionName] = useState<string | null>(null);
  const [isNewSession, setIsNewSession] = useState(true);
  const [connected, setConnected] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  useEffect(() => {
    socket.connect();

    socket.on("connect", () => {
      setConnected(true);
      socket.emit("new_action_event", { action: "init" });
    });

    socket.on("disconnect", () => {
      setConnected(false);
    });

    socket.on("new_action_event", (data: ServerEvent) => {
      switch (data.action) {
        case "init_data":
          setUserId(data.user_id);
          localStorage.setItem("bpmn_user_id", data.user_id);
          localStorage.setItem("bpmn_session_token", data.session_token);
          setSessions(data.sessions);
          break;

        case "session_data":
          setActiveSessionId(data.session_id);
          setSessionName(data.name);
          setCurrentHistory(data.history);
          setIsNewSession(false);
          break;

        case "result":
          setIsLoading(false);
          if (data.session_id && data.session_name) {
            // New session created
            setActiveSessionId(data.session_id);
            setSessionName(data.session_name);
            setIsNewSession(false);
            setSessions((prev) => [
              { session_id: data.session_id!, name: data.session_name! },
              ...prev,
            ]);
          }
          setCurrentHistory((prev) => [
            ...prev,
            { role: "assistant", bpmn_xml: data.bpmn_xml },
          ]);
          break;

        case "error":
          setIsLoading(false);
          // Show error inline in chat history
          setCurrentHistory((prev) => [
            ...prev,
            { role: "assistant", error: data.message },
          ]);
          break;
      }
    });

    return () => {
      socket.off("connect");
      socket.off("disconnect");
      socket.off("new_action_event");
      socket.disconnect();
    };
  }, []);

  const handleSelectSession = useCallback((sessionId: string) => {
    setActiveSessionId(sessionId);
    setIsNewSession(false);
    setSidebarOpen(false);
    socket.emit("new_action_event", {
      action: "open_session",
      session_id: sessionId,
    });
  }, []);

  const handleNewSession = useCallback(() => {
    setActiveSessionId(null);
    setCurrentHistory([]);
    setSessionName(null);
    setIsNewSession(true);
    setSidebarOpen(false);
  }, []);

  const handleSendMessage = useCallback(
    (text: string) => {
      setIsLoading(true);
      setCurrentHistory((prev) => [...prev, { role: "user", text }]);

      socket.emit("new_action_event", {
        action: "message",
        session_id: isNewSession ? null : activeSessionId,
        text,
      });
    },
    [activeSessionId, isNewSession]
  );

  return (
    <div className="app">
      {sidebarOpen && (
        <div className="sidebar-backdrop" onClick={() => setSidebarOpen(false)} />
      )}
      <Sidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        onSelectSession={handleSelectSession}
        onNewSession={handleNewSession}
        isOpen={sidebarOpen}
      />
      <ChatArea
        history={currentHistory}
        onSendMessage={handleSendMessage}
        isLoading={isLoading}
        sessionName={sessionName}
        onMenuToggle={() => setSidebarOpen((v) => !v)}
      />
      {!connected && <div className="connection-status">Connecting...</div>}
    </div>
  );
};

export default App;
