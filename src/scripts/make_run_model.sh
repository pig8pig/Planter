#!/bin/bash
echo 'Don12asa!' | sudo -S make clean
rm /mnt/c/Users/Oxford-Wei/Documents/GitHub/Planter/src/targets/bmv2/software/model_test/test_environment/*.p4
cp /mnt/c/Users/Oxford-Wei/Documents/GitHub/Planter/P4/DT_performance_Iris.p4 /mnt/c/Users/Oxford-Wei/Documents/GitHub/Planter/src/targets/bmv2/software/model_test/test_environment/DT_performance_Iris.p4
echo 'h1 python3 /mnt/c/Users/Oxford-Wei/Documents/GitHub/Planter/src/test/test_switch_model_bmv2_software.py' | sudo -S make run
