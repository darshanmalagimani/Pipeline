#!/usr/bin/env python3
import os
import sys
import subprocess
import shutil
import logging
import platform
import time
import re
from datetime import datetime
from pathlib import Path
import traceback
from minio import Minio
from minio.error import S3Error
from dotenv import load_dotenv
from pymongo import MongoClient

# Load environment variables for MinIO
load_dotenv()

# Define a fixed bucket name for all machines
MINIO_BUCKET_NAME = "hpe-log-analysis"

# Import shared tasks and success-failure check
try:
    from shared_tasks import prepare_machine as shared_prepare_machine
    from shared_tasks import run_log_extraction as shared_run_log_extraction
    # Import determine_update_type_and_check from success-failure.py
    # Using importlib to handle hyphenated filename
    import importlib.util
    spec = importlib.util.spec_from_file_location("success_failure", "success-failure.py")
    success_failure = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(success_failure)
    determine_update_type_and_check = success_failure.determine_update_type_and_check
except ImportError as e:
    logging.error(f"Failed to import modules: {str(e)}. Make sure shared_tasks.py and success-failure.py are in the same directory.")
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format= '%(asctime)s - %(levelname)s - %(message)s  ',
    handlers=[
        logging.FileHandler("project_run.log"),
        logging.StreamHandler()
    ]
)

# ANSI color codes for terminal output
class Colors:
    HEADER =    '\033[95m'
    BLUE =  '\033[94m'
    CYAN =  '\033[96m'
    GREEN =     '\033[92m'
    YELLOW =    '\033[93m'
    RED =   '\033[91m'
    ENDC =  '\033[0m'
    BOLD =  '\033[1m'
    UNDERLINE =     '\033[4m'

def print_section(title):
    """Print a formatted section title"""
    print(f"\n{Colors.HEADER}{Colors.BOLD}{ '=  '*80}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}=== {title} {Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{   '=  '*80}{Colors.ENDC}\n")

def print_step(step):
    """Print a formatted step description"""
    print(f"{Colors.BLUE}{Colors.BOLD}>> {step}{Colors.ENDC}")

def print_success(message):
    """Print a success message"""
    print(f"{Colors.GREEN}{Colors.BOLD}✓ {message}{Colors.ENDC}")

def print_warning(message):
    """Print a warning message"""
    print(f"{Colors.YELLOW}{Colors.BOLD}⚠ {message}{Colors.ENDC}")

def print_error(message):
    """Print an error message"""
    print(f"{Colors.RED}{Colors.BOLD}✗ {message}{Colors.ENDC}")

def run_command(command, shell=False, check=True, cwd=None, capture_output=True):
    """Run a shell command and return the result, logging output."""
    try:
        command_str = command if isinstance(command, str) else  '   '.join(command)
        logging.info(f"Running command: {command_str} in {cwd or os.getcwd()}")
        print_step(f"Running: {command_str}")
        
        result = subprocess.run(
            command,
            shell=shell,
            check=False, # We check manually to log output before raising
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            text=True,
            cwd=cwd
        )
        
        if capture_output:
            if result.stdout:
                logging.info(f"Command stdout:\n{result.stdout.strip()}")
                # Optionally print stdout too
                # print(result.stdout.strip())
            if result.stderr:
                # Log stderr as warning or error based on return code
                log_level = logging.ERROR if result.returncode != 0 else logging.WARNING
                logging.log(log_level, f"Command stderr:\n{result.stderr.strip()}")
                # Optionally print stderr too
                # print_warning(f"Command stderr:\n{result.stderr.strip()}")

        if check and result.returncode != 0:
            error_msg = f"Command failed with code {result.returncode}: {command_str}"
            if capture_output and result.stderr:
                error_msg += f"\nStderr: {result.stderr.strip()}"
            logging.error(error_msg)
            print_error(error_msg)
            raise subprocess.CalledProcessError(result.returncode, command, output=result.stdout, stderr=result.stderr)
            
        logging.info(f"Command finished with code {result.returncode}: {command_str}")
        return result
        
    except subprocess.CalledProcessError as e:
        # Already logged in the check block if check=True
        if not check:
             print_error(f"Command failed with code {e.returncode}:\n{e.stderr}")
        raise # Re-raise the exception
    except Exception as e:
        error_msg = f"Failed to run command     '{command_str}  ': {str(e)}"
        logging.error(error_msg)
        print_error(error_msg)
        traceback.print_exc()
        raise

def setup_virtual_environment():
    """Set up the Python virtual environment and install dependencies"""
    print_section("Setting up Python Virtual Environment")
    
    venv_dir = Path(".venv")
    requirements_file = Path("requirements.txt")
    
    if not requirements_file.exists():
        print_warning(f"'{requirements_file}' not found. Skipping dependency installation.")
        return True # Not necessarily an error, maybe deps are installed globally
        
    try:
        if venv_dir.exists():
            print_step("Virtual environment already exists. Reusing...")
        else:
            print_step("Creating new virtual environment...")
            run_command([sys.executable, "-m", "venv", ".venv"], check=True)
        
        # Determine the correct pip path based on OS
        if platform.system() == "Windows":
            pip_path = str(venv_dir / "Scripts" / "pip.exe")
            # On Windows, running pip directly often works better than activate+pip
            run_command([pip_path, "install", "-r", str(requirements_file)], check=True)
        else:
            pip_path = str(venv_dir / "bin" / "pip")
            # Use the pip from the venv directly
            run_command([pip_path, "install", "-r", str(requirements_file)], check=True)
        
        print_success("Virtual environment setup complete")
        return True
    except Exception as e:
        print_error(f"Failed to set up virtual environment: {str(e)}")
        return False

def get_minio_client():
    """Get a MinIO client with proper error handling"""
    try:
        client = Minio(
            os.getenv("MINIO_ENDPOINT"),
            access_key=os.getenv("MINIO_ACCESS_KEY"),
            secret_key=os.getenv("MINIO_SECRET_KEY"),
            secure=os.getenv("MINIO_SECURE", "True").lower() in ("true", "1", "t"),
        )
        logging.info(f"Successfully connected to MinIO at {os.getenv('MINIO_ENDPOINT')}")
        return client
    except Exception as e:
        logging.error(f"Failed to initialize MinIO client: {str(e)}")
        raise

def sanitize_name(name):
    """
    Sanitize a string to be used as a valid S3 object prefix or bucket name.
    """
    # Convert to lowercase
    name = name.lower()

    # Replace underscores and spaces with hyphens
    name = name.replace("_", "-").replace(" ", "-")

    # Remove any invalid characters (only allow a-z, 0-9, . and -)
    name = re.sub(r"[^a-z0-9.-]", "", name)

    # Replace consecutive hyphens or dots with a single one
    name = re.sub(r"[-]+", "-", name)
    name = re.sub(r"[.]+", ".", name)

    # Remove leading and trailing hyphens and dots
    name = name.strip(".-")

    # Ensure it starts and ends with a letter or number
    if len(name) > 0 and not re.match(r"^[a-z0-9]", name):
        name = "a" + name
    if len(name) > 0 and not re.match(r"[a-z0-9]$", name):
        name = name + "z"

    return name

def upload_to_minio(machine_name, base_output_dir_str="./output"):
    """Upload machine's output (from base_output_dir) to MinIO with error handling."""
    try:
        logging.info(f"Starting MinIO upload for {machine_name}")
        print_step(f"Starting MinIO upload for {machine_name}")

        # Get MinIO client
        client = get_minio_client()

        # Use sanitized machine name as a prefix for organizing files within the bucket
        machine_prefix = sanitize_name(machine_name)
        output_path = os.path.join(base_output_dir_str, machine_name)

        # Ensure the output directory exists (where extracted logs should be)
        if not os.path.isdir(output_path):
            error_msg = f"Output directory not found: {output_path}. Cannot upload. Was log extraction run?"
            logging.error(error_msg)
            print_error(error_msg)
            return False

        logging.info(f"Using prefix '{machine_prefix}' for machine '{machine_name}'")

        # Check if the bucket exists, create if not
        try:
            if not client.bucket_exists(MINIO_BUCKET_NAME):
                client.make_bucket(MINIO_BUCKET_NAME)
                logging.info(f"Created bucket: {MINIO_BUCKET_NAME}")
            else:
                logging.info(f"Using existing bucket: {MINIO_BUCKET_NAME}")
        except Exception as e:
            error_msg = f"Error checking/creating MinIO bucket: {str(e)}"
            logging.error(error_msg)
            print_error(error_msg)
            return False

        # Count files for progress tracking
        total_files = sum(len(files) for _, _, files in os.walk(output_path))
        if total_files == 0:
            warning_msg = f"No files found in {output_path} to upload for {machine_name}."
            logging.warning(warning_msg)
            print_warning(warning_msg)
            return True # Not an error, just nothing to upload
            
        logging.info(f"Found {total_files} files to upload for {machine_name}")
        print_step(f"Found {total_files} files to upload for {machine_name}")

        upload_count = 0
        file_count = 0
        error_count = 0

        # Upload files recursively with error handling
        for root, dirs, files in os.walk(output_path):
            # Process files first
            for file in sorted(files):
                file_count += 1
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, output_path)
                # Create MinIO path with machine prefix
                minio_path = f"{machine_prefix}/{rel_path.replace('\\', '/')}"

                try:
                    # Show progress periodically
                    if file_count % 10 == 0 or file_count == 1 or file_count == total_files:
                        progress_msg = f"Uploading file {file_count}/{total_files} ({file_count/total_files*100:.1f}%): {file}"
                        logging.info(progress_msg)
                        if file_count % 50 == 0 or file_count == 1 or file_count == total_files:
                            print_step(progress_msg)

                    # Upload the file with retry on failure
                    for attempt in range(3):  # Try up to 3 times
                        try:
                            client.fput_object(MINIO_BUCKET_NAME, minio_path, file_path)
                            upload_count += 1
                            break
                        except Exception as e:
                            if attempt < 2:  # Don't log on the last attempt as we'll log after the loop
                                logging.warning(f"Retry {attempt+1}/3 for {file}: {str(e)}")
                                time.sleep(1)  # Small delay before retry
                            else:
                                raise
                except Exception as e:
                    error_msg = f"Failed to upload {file}: {str(e)}"
                    logging.error(error_msg)
                    error_count += 1
                    # Continue with other files

        # Final report
        if upload_count > 0:
            success_rate = (upload_count / total_files) * 100 if total_files > 0 else 0
            result_msg = f"Successfully uploaded {upload_count}/{total_files} files ({success_rate:.1f}%) to MinIO bucket {MINIO_BUCKET_NAME}/{machine_prefix}/"
            logging.info(result_msg)

            # Consider it successful if most files were uploaded
            if success_rate >= 80:
                print_success(result_msg)
                return True
            else:
                warning_msg = f"Upload only partially successful for {machine_name} ({success_rate:.1f}%)"
                logging.warning(warning_msg)
                print_warning(warning_msg)
                return False
        elif total_files > 0: # Only error if there were files to upload
            error_msg = f"No files were successfully uploaded for {machine_name}"
            logging.error(error_msg)
            print_error(error_msg)
            return False
        else: # No files existed, which is fine
            return True

    except Exception as e:
        error_msg = f"Exception in MinIO upload for {machine_name}: {str(e)}"
        logging.error(error_msg)
        print_error(error_msg)
        traceback.print_exc()
        return False

def cleanup_directories(base_source_dir_str="./machines", base_output_dir_str="./output"):
    """Clean up output directories and previously processed data in source dirs"""
    print_section("Cleaning Up Previous Data")
    
    output_dir = Path(base_output_dir_str)
    source_dir = Path(base_source_dir_str)
    
    try:
        # Clean output directory
        if output_dir.exists():
            print_step(f"Removing existing output directory: {output_dir}...")
            shutil.rmtree(output_dir)
        os.makedirs(output_dir, exist_ok=True)
        print_success(f"Cleaned and recreated output directory: {output_dir}")
        
        # Clean required_files directories in machine folders
        if source_dir.is_dir():
            print_step(f"Removing existing 'required_files' directories within {source_dir}...")
            count = 0
            for item in source_dir.glob("**/required_files"): # Recursive search
                if item.is_dir():
                    try:
                        shutil.rmtree(item)
                        logging.info(f"Removed directory: {item}")
                        count += 1
                    except Exception as e:
                        print_warning(f"Could not remove {item}: {str(e)}")
            print_success(f"Removed {count} 'required_files' directories.")
        else:
            print_warning(f"Source directory {source_dir} not found, skipping required_files cleanup.")
            
        return True
    except Exception as e:
        print_error(f"Error during cleanup: {str(e)}")
        traceback.print_exc()
        return False

# Removed local definitions of extract_sdmp_file, find_sdmp_files, prepare_machine, run_log_extraction
# These are now imported from shared_tasks.py

def update_machine_status_counts(success_count, failure_count):
    """Update machine update status counts in MongoDB."""
    try:
        # MongoDB connection using credentials from .env
        client = MongoClient(
            host=os.getenv("MONGO_HOST"),
            port=int(os.getenv("MONGO_PORT")),
            username=os.getenv("MONGO_USER"),
            password=os.getenv("MONGO_PASS")
        )
        
        # Get or create database
        db = client[os.getenv("MONGO_DB")]
        
        # Get or create Analytics collection
        analytics_collection = db["Analytics"]
        
        # Get or create the machine status count document
        status_doc = analytics_collection.find_one({"_id": "Machine update status count"})
        
        if status_doc is None:
            # Create new document if it doesn't exist
            analytics_collection.insert_one({
                "_id": "Machine update status count",
                "successful_updates": success_count,
                "failed_updates": failure_count
            })
        else:
            # Update existing document
            analytics_collection.update_one(
                {"_id": "Machine update status count"},
                {
                    "$inc": {
                        "successful_updates": success_count,
                        "failed_updates": failure_count
                    }
                }
            )
        
        print_success(f"Updated MongoDB analytics: {success_count} successes, {failure_count} failures")
        logging.info(f"Updated MongoDB analytics: {success_count} successes, {failure_count} failures")
        
    except Exception as e:
        error_msg = f"Failed to update MongoDB analytics: {str(e)}"
        print_error(error_msg)
        logging.error(error_msg)
        traceback.print_exc()

def check_firmware_update_status(machine_name, base_output_dir_str="./output"):
    """
    Check if the firmware update was successful or a failure for the given machine.
    Returns True if success, False if failure or error.
    """
    try:
        output_path = os.path.join(base_output_dir_str, machine_name)
        installsetlog_path = os.path.join(output_path, "installSetLogs.log")
        cidebug_path = os.path.join(output_path, "ciDebug.log")
        
        # Check if required log files exist
        if not os.path.isfile(installsetlog_path):
            warning_msg = f"installSetLogs.log not found for {machine_name} at {installsetlog_path}"
            logging.warning(warning_msg)
            print_warning(warning_msg)
            return False
            
        if not os.path.isfile(cidebug_path):
            warning_msg = f"ciDebug.log not found for {machine_name} at {cidebug_path}"
            logging.warning(warning_msg)
            print_warning(warning_msg)
            return False
        
        # Call the determine_update_type_and_check function to check status
        result = determine_update_type_and_check(installsetlog_path, cidebug_path)
        logging.info(f"Firmware update status for {machine_name}: {result}")
        
        # Check if result indicates success (look for "✅" in the result string)
        if "✅" in result:
            success_msg = f"Firmware update was SUCCESSFUL for {machine_name}: {result}"
            logging.info(success_msg)
            print_success(success_msg)
            return True
        else:
            failure_msg = f"Firmware update FAILED for {machine_name}: {result}"
            logging.info(failure_msg)
            print_warning(failure_msg)
            return False
            
    except Exception as e:
        error_msg = f"Error checking firmware update status for {machine_name}: {str(e)}"
        logging.error(error_msg)
        print_error(error_msg)
        traceback.print_exc()
        return False

# Signal file constants
MASTER_SIGNAL_FILE = "NEEDS_MASTER"

def write_master_signal(should_run_master=False):
    """
    Creates or removes a signal file to indicate if master.py should run.
    
    Args:
        should_run_master (bool): If True, creates the signal file. If False, removes it if it exists.
    
    Returns:
        bool: True if operation was successful, False otherwise.
    """
    try:
        signal_file = Path(MASTER_SIGNAL_FILE)
        
        if should_run_master:
            # Create signal file
            signal_file.touch(exist_ok=True)
            msg = f"Created signal file '{MASTER_SIGNAL_FILE}' to indicate master.py should run"
            logging.info(msg)
            print_step(msg)
            return True
        else:
            # Remove signal file if it exists
            if signal_file.exists():
                signal_file.unlink()
                msg = f"Removed signal file '{MASTER_SIGNAL_FILE}' as master.py is not needed"
                logging.info(msg)
                print_step(msg)
            return True
    except Exception as e:
        error_msg = f"Error managing master signal file: {str(e)}"
        logging.error(error_msg)
        print_error(error_msg)
        traceback.print_exc()
        return False

# Removed run_master_process() function as it's no longer called from here.
# def run_master_process(): ...

def main():
    """Main function to run Prep, Extract, and Upload steps."""
    start_time = time.time()
    print_section("HPE Log Processing Project - Preparation, Extraction & Upload")
    print(f"Started at: {datetime.now().strftime(   '%Y-%m-%d %H:%M:%S  ')}")
    
    # Define base directories
    base_source_dir = Path("./machines")
    base_output_dir = Path("./output")
    
    overall_success = True
    success_count = 0  # Track successful updates
    failure_count = 0  # Track failed updates
    
    try:
        # Step 1: Setup environment
        if not setup_virtual_environment():
            print_error("Environment setup failed. Aborting.")
            return False
        
        # Step 2: Clean up old data
        if not cleanup_directories(str(base_source_dir), str(base_output_dir)):
            print_warning("Cleanup failed or partially failed. Continuing cautiously...")
            # Decide if this is critical
            # return False 
        
        # Step 3: Process each machine (Prepare, Extract & Upload)
        print_section("Processing Machine Data (Preparation, Extraction & Upload)")
        
        if not base_source_dir.is_dir():
            print_error(f"Source directory '{base_source_dir}' not found. Cannot process machines.")
            return False
            
        machines = [d.name for d in base_source_dir.iterdir() if d.is_dir()]
        
        if not machines:
            print_warning(f"No machine directories found in {base_source_dir}")
            # Exit cleanly if no machines found
            overall_success = True # No work to do is still a success
        else:
            print_step(f"Found {len(machines)} machines to process: {   ',  '.join(sorted(machines))}")
            
            prep_extract_failures = []
            for machine_name in sorted(machines):
                machine_path = base_source_dir / machine_name
                print_section(f"Processing {machine_name} - Prep, Extract & Upload")
                
                # Prepare the machine using shared function
                print_step(f"Preparing {machine_name}...")
                prep_success = shared_prepare_machine(str(machine_path))
                
                if not prep_success:
                    print_warning(f"Preparation failed for {machine_name}. Skipping extraction.")
                    prep_extract_failures.append(machine_name)
                    continue # Skip to next machine
                else:
                    print_success(f"Preparation successful for {machine_name}.")
                    
                # Run log extraction using shared function
                print_step(f"Running log extraction for {machine_name}...")
                extract_success = shared_run_log_extraction(machine_name, str(base_source_dir), str(base_output_dir))
                
                if not extract_success:
                    print_warning(f"Log extraction failed for {machine_name}.")
                    prep_extract_failures.append(machine_name)
                    # Continue to next machine
                else:
                    print_success(f"Log extraction successful for {machine_name}.")
                    
                    # Check firmware update status
                    print_step(f"Checking firmware update status for {machine_name}...")
                    is_update_success = check_firmware_update_status(machine_name, str(base_output_dir))
                    
                    # Only proceed with upload if update was a failure
                    if is_update_success:
                        success_count += 1  # Increment success counter
                        print_step(f"Skipping MinIO upload for {machine_name} as firmware update was successful.")
                        logging.info(f"Skipped MinIO upload for {machine_name} as firmware update was successful.")
                        
                        # Remove the machine's output directory to prevent master.py from processing it
                        try:
                            machine_output_path = os.path.join(str(base_output_dir), machine_name)
                            if os.path.exists(machine_output_path):
                                print_step(f"Removing output directory for {machine_name} to prevent further processing...")
                                shutil.rmtree(machine_output_path)
                                print_success(f"Successfully removed output directory for {machine_name}")
                                logging.info(f"Removed output directory for {machine_name} at {machine_output_path}")
                            else:
                                print_warning(f"Output directory for {machine_name} not found at {machine_output_path}")
                                logging.warning(f"Output directory for {machine_name} not found at {machine_output_path}")
                        except Exception as e:
                            error_msg = f"Failed to remove output directory for {machine_name}: {str(e)}"
                            print_error(error_msg)
                            logging.error(error_msg)
                            traceback.print_exc()
                    else:
                        # Step 3: Upload to MinIO (only if update failed)
                        failure_count += 1  # Increment failure counter
                        print_step(f"Uploading logs for {machine_name} to MinIO (update failure detected)...")
                        upload_success = upload_to_minio(machine_name, str(base_output_dir))
                        
                        if not upload_success:
                            print_warning(f"MinIO upload failed for {machine_name}.")
                            prep_extract_failures.append(f"{machine_name} (upload)")
                            # Continue to next machine
                        else:
                            print_success(f"MinIO upload successful for {machine_name}.")

            if prep_extract_failures:
                print_warning(f"Preparation or extraction failed for: {', '.join(prep_extract_failures)}")
                overall_success = False # Mark overall run as potentially incomplete

        # Step 4: Master process call REMOVED
        # print_section("Skipping Master Process (Upload & Analysis) as requested.")
        # master_success = run_master_process() # <--- REMOVED THIS CALL
        # if not master_success:
        #     overall_success = False
        
        # Final report
        elapsed_time = time.time() - start_time
        print_section("Preparation, Extraction & Upload Complete")
        print(f"Finished at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Total execution time: {elapsed_time:.2f} seconds ({elapsed_time/60:.2f} minutes)")
        print(f"Output logs (if successful) are located in: {base_output_dir}")
        print(f"Files have also been uploaded to MinIO (if successful).")
        
        # Update the message based on whether master.py should run
        if Path(MASTER_SIGNAL_FILE).exists():
            print(f"Master.py will be executed next (via Jenkinsfile) to analyze failure data.")
        else:
            print(f"Master.py execution will be skipped as no failures were detected or need processing.")
        
        # Update MongoDB with machine update statistics
        if success_count > 0 or failure_count > 0:
            print_section("Updating MongoDB Analytics")
            print_step(f"Recording machine update statistics: {success_count} successes, {failure_count} failures")
            update_machine_status_counts(success_count, failure_count)
        
        # Check if output directory is empty or if there are any machine directories left
        output_dirs = [d for d in base_output_dir.iterdir() if d.is_dir()]
        output_empty = len(output_dirs) == 0
        
        if output_empty:
            print_section("Output Directory Status")
            print_warning("Output directory is empty - no machines to process with master.py")
            print_step("Skipping master.py execution as no failures were detected or all failure data was removed")
            write_master_signal(False)
        elif failure_count > 0:
            print_section("Master Processing Required")
            print_step(f"Signal file created for master.py - {failure_count} machine failures need processing")
            write_master_signal(True)
        else:
            print_section("Master Processing Not Required")
            print_step("All machines were successful or removed - master.py will be skipped")
            write_master_signal(False)
        
        if overall_success:
            print_success("Preparation, Extraction & Upload workflow completed successfully.")
        else:
            print_warning("Preparation, Extraction & Upload workflow completed with one or more failures.")
            
        return overall_success
        
    except Exception as e:
        print_error(f"Project execution (Prep & Extract) failed critically: {str(e)}")
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)


