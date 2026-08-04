#!/bin/bash
echo 'rasperry' | sudo -S make clean
rm /home/pi/Planter/src/targets/bmv2/software/model_test/test_environment/*.p4
cp /home/pi/Planter/P4/DT_performance_Iris.p4 /home/pi/Planter/src/targets/bmv2/software/model_test/test_environment/DT_performance_Iris.p4
echo 'h1 python3 /home/pi/Planter/src/test/test_switch_model_bmv2_software.py' | sudo -S make run
