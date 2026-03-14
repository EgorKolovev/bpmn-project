import React from "react";
import { SessionInfo } from "../types";

interface SidebarProps {
  sessions: SessionInfo[];
  activeSessionId: string | null;
  onSelectSession: (sessionId: string) => void;
  onNewSession: () => void;
}

const Sidebar: React.FC<SidebarProps> = ({
  sessions,
  activeSessionId,
  onSelectSession,
  onNewSession,
}) => {
  return (
    <div className="sidebar">
      <div className="sidebar-header">
        <h2>BPMN Generator</h2>
        <button className="new-session-btn" onClick={onNewSession}>
          + New Session
        </button>
      </div>
      <div className="session-list">
        {sessions.map((session) => (
          <div
            key={session.session_id}
            className={`session-item ${
              activeSessionId === session.session_id ? "active" : ""
            }`}
            onClick={() => onSelectSession(session.session_id)}
          >
            <span className="session-name">{session.name}</span>
          </div>
        ))}
        {sessions.length === 0 && (
          <div className="no-sessions">No sessions yet</div>
        )}
      </div>
    </div>
  );
};

export default Sidebar;
