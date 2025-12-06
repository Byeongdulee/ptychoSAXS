import paramiko
from scp import SCPClient
import sys

def scp_file_with_password(hostname, port, username, password, local_path, remote_path):
    """
    Transfers a file to a remote server using SCP with a password.

    Args:
        hostname (str): The hostname or IP address of the remote server.
        port (int): The SSH port (usually 22).
        username (str): The username for SSH authentication.
        password (str): The password for SSH authentication.
        local_path (str): The path to the local file to transfer.
        remote_path (str): The destination path on the remote server.
    """
    try:
        # Create an SSHClient instance
        ssh = paramiko.SSHClient()

        # Set the policy for missing host keys (AutoAddPolicy is for convenience,
        # in production, you might want to use WarningPolicy or RejectPolicy)
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # Connect to the remote server
        ssh.connect(hostname, port, username, password)

        # Create an SCPClient instance
        with SCPClient(ssh.get_transport()) as scp:
            # Put the local file to the remote server
            scp.put(local_path, remote_path)
        
        print(f"File '{local_path}' successfully transferred to '{remote_path}' on {hostname}.")

    except paramiko.AuthenticationException:
        print("Authentication failed. Check your username and password.")
    except paramiko.SSHException as e:
        print(f"SSH connection error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    finally:
        if 'ssh' in locals() and ssh.get_transport() is not None:
            ssh.close()

# SCP transfer using private key authentication
# 1. Generate SSH Keys on Windows:
#   If you don't already have them, generate an SSH key pair using ssh-keygen in a terminal (e.g., Git Bash, WSL, or Command Prompt if you have OpenSSH client installed).
#       ssh-keygen -t rsa -b 4096
#   Follow the prompts. You can choose to set a passphrase for your private key, but for fully passwordless operation, leave it empty. This will create id_rsa (private key) and id_rsa.pub (public key) in your ~/.ssh directory (e.g., C:\Users\YourUser\.ssh).
# 2. Copy Public Key to Remote Server:
#   Transfer your public key (id_rsa.pub) to the remote server and append it to the ~/.ssh/authorized_keys file for the target user. You can do this manually or using ssh-copy-id if available (e.g., through WSL).
#
# private key authentication is generated between sec12pc02 and green.xray.aps.anl.gov for s12idc. 
#
def scp_file(local_path, remote_path="/home/beams/WEB12IDB/www/userData/"):
    # Remote server details
    hostname='green.xray.aps.anl.gov', 
    username = 's12idc'
    private_key_path = f'C:\\Users\\{username}\\.ssh\\id_rsa' # Path to your private key on Windows

    # Create an SSH client
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy()) # Be cautious with AutoAddPolicy in production

    try:
        # Load the private key
        private_key = paramiko.RSAKey.from_private_key_file(private_key_path)

        # Connect to the remote server using the private key
        ssh.connect(hostname=hostname, username=username, pkey=private_key)

        # Create an SCPClient instance
        with SCPClient(ssh.get_transport()) as scp:
            # Put the local file to the remote server
            scp.put(local_path, remote_path)

        print(f"File '{local_path}' uploaded to '{remote_path}' successfully.")

        # Download the file (example)
        # sftp.get(remote_file_path, 'downloaded_file.txt')
        # print(f"File '{remote_file_path}' downloaded to 'downloaded_file.txt' successfully.")

    except paramiko.AuthenticationException:
        print("Authentication failed. Check your username and password.")
    except paramiko.SSHException as e:
        print(f"SSH connection error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    finally:
        if 'ssh' in locals() and ssh.get_transport() is not None:
            ssh.close()

# Example usage:
if __name__ == "__main__":

    args = sys.argv[1:]  # exclude script name

    if len(args) == 5:
        # hostname username password local_path remote_path (use default port 22)
        hostname, username, password, local_path, remote_path = args
        scp_file_with_password(hostname, 22, username, password, local_path, remote_path)
    elif len(args) == 6:
        # hostname port username password local_path remote_path
        hostname, port_str, username, password, local_path, remote_path = args
        try:
            port = int(port_str)
        except ValueError:
            print("Port must be an integer.")
            sys.exit(1)
        scp_file_with_password(hostname, port, username, password, local_path, remote_path)
    elif len(args) == 1:
        # only local_path provided, use default remote_path
        local_path = args[0]
        scp_file(local_path)
    elif len(args) == 2:
        # local_path and remote_path provided
        local_path, remote_path = args
        scp_file(local_path, remote_path)
    else:
        print(
            "Usage:\n"
            " 1) scptransfer.py <local_path>                                    # use scp_file with default remote\n"
            " 2) scptransfer.py <local_path> <remote_path>                      # use scp_file\n"
            " 3) scptransfer.py <hostname> <username> <password> <local> <remote>       # use scp_file_with_password (port=22)\n"
            " 4) scptransfer.py <hostname> <port> <username> <password> <local> <remote> # use scp_file_with_password\n"
        )
        sys.exit(1)