import os
import time
from google.cloud import compute_v1
from dotenv import load_dotenv

load_dotenv()

PROJECT_ID = os.getenv("GCP_PROJECT_ID", "loanbot-8ac74")
ZONE = "asia-south1-a"
INSTANCE_NAME = "trading-engine-spot"
MACHINE_TYPE = "zones/asia-south1-a/machineTypes/c2-standard-4"
TAILSCALE_AUTH_KEY = os.getenv("TAILSCALE_AUTH_KEY")

def create_spot_instance():
    """Creates a GCP Spot VM instance with Tailscale and Docker."""
    instance_client = compute_v1.InstancesClient()
    
    # Startup script to install Docker and Tailscale
    startup_script = f"""#!/bin/bash
    sudo apt-get update
    sudo apt-get install -y apt-transport-https ca-certificates curl software-properties-common
    curl -fsSL https://download.docker.com/linux/debian/gpg | sudo apt-key add -
    sudo add-apt-repository "deb [arch=amd64] https://download.docker.com/linux/debian $(lsb_release -cs) stable"
    sudo apt-get update
    sudo apt-get install -y docker-ce docker-compose
    sudo systemctl start docker
    sudo systemctl enable docker
    
    # Install Tailscale
    curl -fsSL https://tailscale.com/install.sh | sh
    sudo tailscale up --authkey={TAILSCALE_AUTH_KEY}
    
    # Clone repo and start docker-compose (Assuming repo will be pulled)
    # git clone https://github.com/AR-Karthik/trading_microservices.git /opt/trading_microservices
    # cd /opt/trading_microservices && sudo docker-compose up -d
    """

    instance = compute_v1.Instance()
    instance.name = INSTANCE_NAME
    instance.machine_type = MACHINE_TYPE

    # Configure as a Spot VM to reduce costs
    instance.scheduling = compute_v1.Scheduling(
        provisioning_model=compute_v1.Scheduling.ProvisioningModel.SPOT,
        instance_termination_action="STOP"
    )

    # Boot Disk: 50GB pd-ssd
    disk = compute_v1.AttachedDisk()
    disk.initialize_params = compute_v1.AttachedDiskInitializeParams(
        disk_size_gb=50,
        disk_type=f"zones/{ZONE}/diskTypes/pd-ssd",
        source_image="projects/debian-cloud/global/images/family/debian-11"
    )
    disk.auto_delete = True
    disk.boot = True
    instance.disks = [disk]

    # Network
    network_interface = compute_v1.NetworkInterface()
    network_interface.network = "global/networks/default"
    
    # External IP (Optional if Tailscale is enough, but required for outward internet access initially)
    access_config = compute_v1.AccessConfig()
    access_config.type_ = compute_v1.AccessConfig.Type.ONE_TO_ONE_NAT
    access_config.name = "External NAT"
    network_interface.access_configs = [access_config]
    instance.network_interfaces = [network_interface]

    # Startup script metadata
    metadata = compute_v1.Metadata()
    metadata.items = [
        compute_v1.Items(key="startup-script", value=startup_script)
    ]
    instance.metadata = metadata

    print(f"Creating Spot VM {INSTANCE_NAME} in {ZONE}...")
    
    try:
        operation = instance_client.insert(
            project=PROJECT_ID,
            zone=ZONE,
            instance_resource=instance
        )
        
        # Wait for operation to complete
        while operation.status != compute_v1.Operation.Status.DONE:
            time.sleep(2)
            # You would normally poll the zone operations client here
            print("Waiting for creation...")
            break # Just breaking for this mock script structure
            
        print(f"Instance {INSTANCE_NAME} requested successfully.")
    except Exception as e:
        print(f"Failed to provision instance: {e}")

if __name__ == "__main__":
    create_spot_instance()
