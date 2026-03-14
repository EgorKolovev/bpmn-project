import React, { useState, useRef, useEffect } from "react";
import { HistoryEntry } from "../types";
import MessageBubble from "./MessageBubble";

interface ChatAreaProps {
  history: HistoryEntry[];
  onSendMessage: (text: string) => void;
  isLoading: boolean;
  sessionName: string | null;
}

const ChatArea: React.FC<ChatAreaProps> = ({
  history,
  onSendMessage,
  isLoading,
  sessionName,
}) => {
  const [inputText, setInputText] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [history]);

  const resizeTextarea = () => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = textarea.scrollHeight + "px";
  };

  useEffect(() => {
    resizeTextarea();
  }, [inputText]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const text = inputText.trim();
    if (!text || isLoading) return;
    onSendMessage(text);
    setInputText("");
    // Reset textarea height after send
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  return (
    <div className="chat-area">
      {sessionName && <div className="chat-header">{sessionName}</div>}
      <div className="messages-container">
        {history.length === 0 && (
          <div className="empty-chat">
            <h3>BPMN Diagram Generator</h3>
            <p>Describe a business process to generate a BPMN diagram.</p>
            <p>Then send follow-up messages to edit the diagram.</p>
          </div>
        )}
        {history.map((entry, index) => (
          <MessageBubble key={index} entry={entry} />
        ))}
        {isLoading && (
          <div className="message message-assistant">
            <div className="message-content loading">
              <div className="loading-dots">
                <span></span><span></span><span></span>
              </div>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>
      <form className="input-area" onSubmit={handleSubmit}>
        <textarea
          ref={textareaRef}
          value={inputText}
          onChange={(e) => setInputText(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={
            history.length === 0
              ? "Describe a business process..."
              : "Describe changes to the diagram..."
          }
          rows={1}
          disabled={isLoading}
        />
        <button type="submit" disabled={isLoading || !inputText.trim()}>
          Send
        </button>
      </form>
    </div>
  );
};

export default ChatArea;
