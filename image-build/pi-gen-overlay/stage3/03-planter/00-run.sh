#!/bin/bash -e
on_chroot << EOF
pip3 install scikit-learn numpy pandas scapy xgboost matplotlib joblib pydotplus packaging jsonschema seaborn tqdm ipython wget category_encoders 2>/dev/null || true
sudo pip3 install numpy scikit-learn scapy 2>/dev/null || true
cd /home/pi
git clone -b gsoc-p4c-dpdk https://github.com/pig8pig/Planter.git
chown -R pi:pi Planter
if [ -d /usr/share/dpdk/examples/pipeline ]; then
    cp -r /usr/share/dpdk/examples/pipeline /home/pi/dpdk_pipeline_build
    cd /home/pi/dpdk_pipeline_build
    sed -i '/params.file_name = tokens\[t0 + 2\];/a\\t\t\tparams.n_pkts_max = 0;' cli.c
    make && echo "dpdk-pipeline built OK" || echo "WARNING: build failed"
    chown -R pi:pi /home/pi/dpdk_pipeline_build
fi
systemctl disable t4p4s 2>/dev/null || true
apt-get install -y mininet --fix-missing 2>/dev/null || true
MAKEFILE="/home/pi/Planter/src/targets/bmv2/software/utils/Makefile"
[ -f "\$MAKEFILE" ] && sed -i 's|/usr/local/lib/python3.12/site-packages|/usr/lib/python3/dist-packages|g' "\$MAKEFILE"
echo "Planter GSoC 2026 install complete"
EOF
