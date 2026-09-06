import { initializeProject } from "../services/project-initialization.js";


export function runInit(directory: string): void {
  const project = initializeProject(directory);

  console.log(
    `Initialized Crucible project ${project.id} at ${project.root}`,
  );
}
