import subprocess
import json
import time


def main():
    board = "jarvis-os"
    assignee_to_watch = "elon"
    watch_duration_seconds = 295

    print(
        f"Starting Kanban watcher for assignee '{assignee_to_watch}' on board '{board}' for {watch_duration_seconds} seconds..."
    )

    command = ["hermes", "kanban", "watch", "--board", board, "--json"]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )

    start_time = time.time()

    if process.stdout is None:
        print("Failed to capture stdout from hermes kanban watch")
        return

    try:
        for line in iter(process.stdout.readline, ""):
            if time.time() - start_time > watch_duration_seconds:
                print("Watcher timeout reached.")
                break
            try:
                event = json.loads(line)
                if "task" in event and "payload" in event:
                    task = event["task"]
                    payload = event["payload"]

                    if payload.get("assignee") == assignee_to_watch and (
                        task.get("status") == "ready" or task.get("status") == "todo"
                    ):
                        task_id = task.get("id")
                        if task_id:
                            print(
                                f"New task assigned to {assignee_to_watch}: {task_id}"
                            )
                            # Use 'hermes chat' with a one-shot prompt to work the task
                            # This is a more robust way to ensure the elon profile is used
                            # and the task is worked in a proper hermes session.
                            work_command = (
                                f"hermes -p elon --yolo -z 'work kanban task {task_id}'"
                            )
                            subprocess.run(work_command, shell=True, check=True)

            except json.JSONDecodeError:
                # Ignore lines that are not valid JSON
                pass
            except Exception as e:
                print(f"An error occurred: {e}")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
            print("Watcher process terminated.")


if __name__ == "__main__":
    main()
