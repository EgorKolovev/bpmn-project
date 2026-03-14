import React from "react";
import { HistoryEntry } from "../types";
import BpmnViewer from "./BpmnViewer";

interface MessageBubbleProps {
  entry: HistoryEntry;
}

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

  return (
    <div className="message message-assistant">
      <div className="message-content">
        {entry.bpmn_xml && <BpmnViewer xml={entry.bpmn_xml} />}
      </div>
    </div>
  );
};

export default MessageBubble;
