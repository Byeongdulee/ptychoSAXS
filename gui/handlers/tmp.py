
    def fly0(self, motornumber=-1, update_progress=None, update_status=None):
        t0 = time.time()
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        self.plotlabels = []
        if self.ui.actionckTime_reset_before_scan.isChecked():
            if s12softglue.isConnected:
                s12softglue.ckTime_reset()
        if self.ui.actionMemory_clear_before_scan.isChecked():
            try:
                if s12softglue.isConnected:
                    s12softglue.memory_clear()
            except TimeoutError:
                self.messages["recent error message"] = "softglue memory_clear timeout"
                print(self.messages["recent error message"])

        print("")
        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            print("**** Test Run:")
        self.isfly = True
        self.isscan = True

        # disable fit menu
        self.ui.actionFit_QDS_phi.setEnabled(False)

        if not self.ui.cb_keepprevscan.isChecked():
            self.clearplot()
        
        st = self.fly1d_st + self.fly1d_p0
        fe = self.fly1d_fe + self.fly1d_p0
        step = self.fly1d_step
        tm = self.fly1d_tm

        pos = self.pts.get_pos(axis)
        #print("Time to finish line 2127: %0.3f" % (time.time()-t0)) very fast down to this far
        if (axis in self.pts.hexapod.axes) and (self.hexapod_flymode==HEXAPOD_FLYMODE_WAVELET):
            if self.ui.cb_reversescandir.isChecked():
                if abs(st-pos)>abs(fe-pos):
                    t = fe
                    fe = st
                    st = t 
                    step = -step
            direction = int(step)/abs(step)
            if direction==1:
                dirv = 0
            else:
                dirv = 6
            self.pts.hexapod.assign_axis2wavtable(axis, self.pts.hexapod.WaveGenID[axis]+dirv)

            period = self.pts.hexapod.pulse_step  # pulse step time.
            expt = period*self.parameters._ratio_exp_period # JMM, *0.2 previously for JD. -0.02 previously for BL
            if isTestRun:
                print(f"{self.pts.hexapod.pulse_number} images will be collected every {period}s with exposure time of {expt}s.")
            
            if period-expt < DETECTOR_READOUTTIME:
                self.messages["recent error message"] = (
                    f"Exposure time {expt:.4f} and period {period:.4f} requires the readout time {period-expt}, which is too short."
                )
                print(self.messages["recent error message"])
                self.ui.statusbar.showMessage(self.messages["recent error message"])
                return None

            if expt <= 0:
                self.messages["recent error message"] = f"Note that after subtracting the detector readout time {self.det_readout_time:.3e} s, the exposure time becomes equal or less than 0."
                print(self.messages["recent error message"])
                raise DET_MIN_READOUT_Error(self.messages["recent error message"])
            
            if abs(period) < 0.033:
                self.messages["recent error message"] = f"Note that Max speed of Pilatus2M is 30Hz."
                print(self.messages["recent error message"])
                raise DET_OVER_READOUT_SPEED_Error(self.messages["recent error message"])

            # set the delay generator
            if expt != dg645_12ID._exposuretime:
                try:
                    dg645_12ID.set_pilatus_fly(expt)
                except:
                    raise DG645_Error

            #SoftGlue ready for recording interferometer values
            movestep = abs(fe-st)/self.pts.hexapod.pulse_number*1000*self.parameters._ratio_exp_period
            print(f"Actual exposure time: {expt:0.3e} s, during which {axis} will move {movestep:.3e} um.")

            # If softglue SG is not selected, use prepare for the softglue.
            if self.detector[3] is None: 
                if s12softglue.isConnected:
                    N_counts = s12softglue.number_acquisition(expt, self.pts.hexapod.pulse_number)
                    self.parameters.countsperexposure = np.round(N_counts/self.pts.hexapod.pulse_number)
                    print(f"Total {self.parameters.countsperexposure} encoder positions will be collected per a DET image.")
                    if N_counts>100000:
                        self.messages["recent error message"] = f"******** CAUTION: Number of softglue counts: {N_counts} is larger than 100E3. Slow down the clock speed."
                        raise SOFTGLUE_Setup_Error(self.messages["recent error message"])

            if isTestRun:
                return
            
            # Scan start ............................
            self.pts.hexapod.goto_start_pos(axis) # took 0.4 second
            for detN, det in enumerate(self.detector):
                if det is not None:
                    try:
                        det.fly_ready(expt, self.pts.hexapod.pulse_number, period=period, 
                                        isTest = isTestRun, capture=(self.use_hdf_plugin, self.hdf_plugin_savemode), fn=self.hdf_plugin_name[detN])
                    except TimeoutError:
                        self.messages["recent error message"] = f"Detector, {det._prefix}, hasnt started yet. Fly scan will not start."
                        print(self.messages["recent error message"])
                        self.ui.statusbar.showMessage(self.messages["recent error message"])
                        return DETECTOR_NOT_STARTED_ERROR
            print("Ready for traj")
            pos = self.pts.get_pos(axis)
            print(f"pos is {pos} before traj run start.")

            timeout_occurred, TIMEOUT = self.is_arming_detecotors_timedout()
            if timeout_occurred:
                self.messages["recent error message"] = f"Timeout occurred after {TIMEOUT} seconds while waiting for detector to be Armed. {time.ctime()}"
                print(self.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR
        
            istraj_running = False
            timeout = 5
            i = 0
            print("Trajectory scan initiated..")
            while not istraj_running:
                try:
                    self.pts.hexapod.run_traj(axis)
                except:
                    pass
                time.sleep(0.05)
                pos_tmp = self.pts.get_pos(axis)
                if pos_tmp != pos:
                    istraj_running = True
                #istraj_running = self.is_traj_running()
                i = i+1
                if i>timeout:
                    self.messages["recent error message"] = "traj scan command is resent for 5 times to the hexapod without success."
                    print(self.messages["recent error message"])
                    break
            print("Run_traj is sent command in rungui.")
            isattarget = False
            timeelapsed = 0
            t0 = time.time()
            while not isattarget:
                try:
                    isattarget = self.pts.hexapod.isattarget(axis)
                except:
                    isattarget = False
                time.sleep(0.02)
                #pos_tmp = self.pts.get_pos(axis)
                timeelapsed = time.time()-t0
                prog = float(timeelapsed)/float(tm)
                if update_progress:
                    update_progress(int(prog*100))
                msg1 = f'Elapsed time = {int(timeelapsed)}s since the start.'
                if prog>0:
                    remainingtime = timeelapsed/prog - timeelapsed
                else:
                    remainingtime = 999
                msg2 = f"; Remaining time for the current 2D scan is {np.round(remainingtime,2)}s\n"
                self.messages["current status"] = "%s%s"%(msg1, msg2)
                if update_status:
                    update_status(self.messages["current status"])

                if self.isStopScanIssued:
                    break

            pos = self.pts.get_pos(axis)
            print(f"pos is {pos:.3e} after the traj run done.")
        # fly scan with a constant velocity of motions.
        else: 
            print("Fly scan with phi.")
            # fly for phi scan is unique.
            # tm is the total time for the fly scan, which is determined by the user input.
            # step is the angle step, which is determined by the user input.
            Xstep = self.fly1d_step  # step angle (this was step time before)
            # This was the total time before, but now we will use it as the exposure time
            # a time for each step will be calculated.
            Xtm = self.fly1d_tm

            # step time calculation
            step_time  = Xtm + self.det_readout_time
            if step_time < 0.033:
                step_time = 0.033
            #self.parameters._ratio_exp_period = Xtm / step_time
            # total time calculation
            Nsteps = int((fe-st)/Xstep)
            total_time = Nsteps * step_time
            #expt = step_time*self.parameters._ratio_exp_period # JMM, *0.2 previously for JD. -0.02 previously for BL
            expt = Xtm
            if step_time - expt < 0.015:
                raise DET_MIN_READOUT_Error(f"Period - Exposure Time,{step_time-expt}s, should be longer than 50 microseconds.")

            # set the delay generator
            try:
                dg645_12ID.set_pilatus2(expt, Nsteps, step_time)  # exposuretime, number of images, and time period for fly scan.
            except:
                raise DG645_Error
            print(f"Exposure time: {expt:0.3e} s, number of steps: {Nsteps}, Step time: {step_time:.3e} s, Total time for the scan: {total_time:.3f} s.")
            if self.ui.cb_reversescandir.isChecked():
                if abs(st-pos)>abs(fe-pos):
                    t = fe
                    fe = st
                    st = t 

            if motornumber ==6:
                # enable fit menu
                self.ui.actionFit_QDS_phi.setEnabled(True)

            self._prev_vel,self._prev_acc = self.pts.get_speed(axis)
            self.pts.mv(axis, st, wait=True)
            time.sleep(0.1)
            #print(f"Setting speed for fly scan. Total time: {abs(fe-st)/total_time:.3f} s, acceleration: {abs(fe-st)/total_time*10:.3f}.")
            self.pts.set_speed(axis, abs(fe-st)/total_time, abs(fe-st)/total_time*10)
            time.sleep(0.02)

            # Need to make detectors ready
            for detN, det in enumerate(self.detector):
                if det is not None:
                    try:
                        det.fly_ready(expt, Nsteps, period=step_time, 
                                        isTest = isTestRun, capture=(self.use_hdf_plugin, self.hdf_plugin_savemode),fn=self.hdf_plugin_name[detN])
            #            print("Time to finish line 2190: %0.3f" % (time.time()-t0)) # take 0.3 second
                    except TimeoutError:
                        self.messages["recent error message"] = f"Detector, {det._prefix}, hasnt started yet. Fly scan will not start."
                        print(self.messages["recent error message"])
                        self.ui.statusbar.showMessage(self.messages["recent error message"])
                        #showerror("Detector timeout.")
                        return            

            timeout_occurred, TIMEOUT = self.is_arming_detecotors_timedout()
            if timeout_occurred:
                self.messages["recent error message"] = f"Timeout occurred after {TIMEOUT} seconds while waiting for detector to be Armed. {time.ctime()}"
                print(self.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR

            scaninfo = []
            print("")
            print(f"{axis} scan started..")
            scaninfo.append(f"FileIndex, {axis},    time(s)")
            scaninfo.append(f'0,   {st},   {time.time()}')
            self.pts.mv(axis, fe, wait=False)
            
            print("about to send out trigger.")
            # Start collect data while an axis is moving.
            dg645_12ID.trigger()
            print("Delay generator is triggered to start the fly scan.")
            # Update progress bar and status message.
            N_imgcollected = 0
            timeelapsed = time.time()-t0
            TIMEOUT = total_time+5
            if TIMEOUT < 5:
                TIMEOUT = 5
            timestart = time.time()
            val = 0
            #print(N_imgcollected, Nsteps)
            while N_imgcollected<Nsteps:
                for ndet, det in enumerate(self.detector):
                    if ndet>1: 
                        continue
                    if det is not None:
                        val = det.ArrayCounter_RBV
                        break
                prog = float(val)/float(Nsteps)
                pos = self.pts.get_pos(axis)
                scaninfo.append(f'{val},    {pos},  {time.time()}')
                
                if update_progress:
                    update_progress(int(prog*100))
                msg1 = f'Elapsed time = {int(timeelapsed)}s since the start.'
                if prog>0:
                    remainingtime = timeelapsed/prog - timeelapsed
                else:
                    remainingtime = 999
                msg2 = f"; Remaining time for the current 2D scan is {np.round(remainingtime,2)}s\n"
                self.messages["current status"] = "%s%s"%(msg1, msg2)
                if update_status:
                    update_status(self.messages["current status"])

                time.sleep(0.1)
                if val>N_imgcollected:
                    N_imgcollected = val
                    timestart = time.time()

                updatetime = time.time()-timestart
                if updatetime>TIMEOUT:
                    self.messages["recent error message"] = f"Detector {det._prefix} data collection timeout after {TIMEOUT} seconds."
                    print(self.messages["recent error message"])
                    self.ui.statusbar.showMessage(self.messages["recent error message"])
                    return DETECTOR_NOT_STARTED_ERROR
                timeelapsed = time.time()-t0
                if self.isStopScanIssued:
                    break
            self.write_scaninfo_to_logfile(scaninfo)

        return 1
