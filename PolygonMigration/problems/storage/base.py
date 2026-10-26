from abc import ABC, abstractmethod


class StorageBackend(ABC):
    """
    Provider-agnostic interface for test-case file storage.

    Paths are '/'-separated keys relative to the provider's configured root
    (container, bucket or folder). Layout for a problem:
        test_cases/{problem_id}/{test_number}      input
        test_cases/{problem_id}/{test_number}.a    output
    """

    @abstractmethod
    def upload_file(self, path, data):
        """Create or overwrite the file at `path` with `data` (bytes). Raises on failure."""

    @abstractmethod
    def delete_prefix(self, prefix):
        """Delete every file under `prefix` (must end with '/'). No-op if nothing exists."""

    @staticmethod
    def problem_prefix(problem_id):
        return f"test_cases/{problem_id}/"

    def upload_test_case(self, problem_id, test_number, input_data, output_data):
        base = f"{self.problem_prefix(problem_id)}{test_number:02d}"
        self.upload_file(base, input_data.encode('utf-8'))
        self.upload_file(f"{base}.a", output_data.encode('utf-8'))

    def delete_problem_files(self, problem_id):
        self.delete_prefix(self.problem_prefix(problem_id))
