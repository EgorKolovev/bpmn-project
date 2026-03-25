import React from "react";
import { HistoryEntry } from "../types";
import BpmnViewer from "./BpmnViewer";

interface MessageBubbleProps {
  entry: HistoryEntry;
}

const AlertIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#b91c1c" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="10" />
    <line x1="12" y1="8" x2="12" y2="12" />
    <line x1="12" y1="16" x2="12.01" y2="16" />
  </svg>
);

const MessageBubble: React.FC<MessageBubbleProps> = ({ entry }) => {
  if (entry.role === "user") {
    return (
      <div className="message message-user">
        <div className="message-content">
          <p>{entry.text}</p>
        </div>
      </div>
    );
  }

  if (entry.error) {
    return (
      <div className="message message-error">
        <div className="message-content">
          <div className="error-message-text">
            <AlertIcon />
            <span>{entry.error}</span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="message message-assistant">
      <div className="message-content">
        {entry.bpmn_xml && <BpmnViewer xml={entry.bpmn_xml} />}
      </div>
    </div>
  );
};

export default MessageBubble;
