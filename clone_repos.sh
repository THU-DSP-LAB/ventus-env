#!/bin/bash

# Initialize variables
download=1  # By default, the repository is cloned.
force=0     # By default, overwriting is not forced (only valid when download=1).
depth=1     # Shallow clone depth.
all_success=1  # Track if all repositories were processed successfully.

# Repository list: The format is "[repository URL] [local directory name] [branch name]".
REPOS=(
  "https://github.com/THU-DSP-LAB/llvm-project.git llvm test_env"
  "https://github.com/THU-DSP-LAB/pocl.git pocl test_env"
  "https://github.com/OCL-dev/ocl-icd.git ocl-icd"
  "https://github.com/THU-DSP-LAB/ventus-driver.git driver test_env"
  "https://github.com/THU-DSP-LAB/ventus-gpgpu-isa-simulator.git spike test_env"
  "https://github.com/THU-DSP-LAB/ventus-gpgpu.git gpgpu test_env"
  "https://github.com/accellera-official/systemc.git systemc 2.3.4"
  "https://github.com/THU-DSP-LAB/ventus-gpgpu-cpp-simulator.git cyclesim test_env"
  "https://github.com/THU-DSP-LAB/gpu-rodinia.git rodinia test_env"
)

# Dataset configuration
DATASET_URL="http://dspdev.ime.tsinghua.edu.cn/images/ventus_dataset/ventus_rodinia_data.tar.xz"
DATASET_FILE=$(basename "$DATASET_URL")
TEMP_DIR="rodinia-dataset"

# Defines functions for processing a single repository.
process_repo() {
  local repo="$1"
  local download="$2"
  local force="$3"
  local depth="$4"

  local parts=($repo)
  local url=${parts[0]}
  local dir=${parts[1]}
  local branch=${parts[2]:-}

  echo "Repository: $url"
  echo "Directory: $dir"
  [ -n "$branch" ] && echo "Specified branch: $branch"

  # Build clone command.
  local clone_cmd="git clone --depth $depth --progress"
  [ -n "$branch" ] && clone_cmd="$clone_cmd -b $branch"
  clone_cmd="$clone_cmd \"$url\" \"$dir\""

  if [ $download -eq 1 ]; then
    # Execution mode: Process directories and clones.
    if [ -d "$dir" ]; then
      if [ $force -eq 1 ]; then
        echo "Warning: Directory $dir already exists, will be forcibly deleted..."
        rm -rf "$dir" || { echo "Error: Unable to remove directory $dir" >&2; return 1; }
      else
        echo "Skipping: Directory $dir already exists (add -f option to force overwrite)"
        echo "----------------------------------------"
        return 0
      fi
    fi

    echo "Starting cloning..."
    eval $clone_cmd
    if [ $? -eq 0 ]; then
      echo "Success：$url cloning completed"
      # Process dataset only for rodinia repository
      if [ "$dir" = "rodinia" ]; then
        process_dataset "$dir" || return 1
      fi
      return 0
    else
      echo "Failure：$url cloning error" >&2
      return 1
    fi
  else
    echo "Plan execution command: $clone_cmd"
    if [ "$dir" = "rodinia" ]; then
      echo "Plan: For rodinia, download dataset from $DATASET_URL and move to $dir/data"
    fi
    [ $force -eq 1 ] && echo "(Preview: If executed, the existing directory will be forcibly overwritten)"
    return 0
  fi

  echo "----------------------------------------"
}

process_dataset() {
  local target_dir="$1"
  local data_dir="$target_dir/data"

  echo "----------------------------------------"
  echo "Processing dataset for $target_dir..."

  echo "Downloading dataset..."
  wget "$DATASET_URL" -O "$DATASET_FILE" || { echo "Error: Dataset download failed" >&2; return 1; }

  echo "Extracting dataset..."
  mkdir -p "$TEMP_DIR"
  tar -xf "$DATASET_FILE" -C "$TEMP_DIR" || { echo "Error: Dataset extraction failed" >&2; rm -f "$DATASET_FILE"; return 1; }

  mkdir -p "$data_dir" && mv "$TEMP_DIR/rodinia/data"/* "$data_dir/" || {
    echo "Error: Failed to move dataset" >&2;
    rm -rf "$TEMP_DIR" "$DATASET_FILE";
    return 1;
  }

  rm -rf "$TEMP_DIR" "$DATASET_FILE" && echo "Temporary files cleaned"

  echo "Dataset processed successfully to $data_dir"
  echo "----------------------------------------"
  return 0
}

# Parse command-line options.
while getopts "df" opt; do
  case $opt in
    d) download=0 ;;  # Disable download mode
    f)
      # -f is only valid in download mode.
      if [ $download -eq 1 ]; then
        force=1
      else
        echo "Warning: -f option is only valid in -d (download mode) and is ignored in preview mode" >&2
      fi
      ;;
    \?) echo "Invalid option: -$OPTARG" >&2; exit 1 ;;
  esac
done

# Displays the script execution mode.
if [ $download -eq 1 ]; then
  echo "=== Execution mode: actual cloning of the repository ==="
  echo "Force overwriting of existing directory: $( [ $force -eq 1 ] && echo "Yes" || echo "No" )"
else
  echo "=== Preview mode: Displays only the plan ==="
fi
echo "Shallow clone depth: $depth"
echo "----------------------------------------"

# Traverse the code repository list.
for repo in "${REPOS[@]}"; do
  if ! process_repo "$repo" "$download" "$force" "$depth"; then
    all_success=0
    if [ $download -eq 1 ]; then  # Only abort on error in download mode
      echo "Aborting due to repository clone failure" >&2
      exit 1
    fi
  fi
done

# Output final result based on overall success
if [ $all_success -eq 1 ]; then
  echo "All repositories have been processed successfully."
else
  echo "Some repositories encountered errors during processing." >&2
  exit 1
fi
