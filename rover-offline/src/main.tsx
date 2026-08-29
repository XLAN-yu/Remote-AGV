import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RobotControlApp } from "../../app/RobotControlApp";
import "./base.css";

const root = document.getElementById("root");

if (!root) {
  throw new Error("ROVER ONE root element is missing");
}

createRoot(root).render(
  <StrictMode>
    <RobotControlApp />
  </StrictMode>,
);
